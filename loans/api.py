import hashlib

from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import services
from .models import Applicant, Assessment, AuditEntry, Document, LoanApplication
from .services import NotAllowed, WorkflowError, roles
from .validators import parse_sa_id

STAFF = {'consultant', 'credit_officer', 'senior_credit_officer', 'operations', 'superuser'}


class ApplicantIn(serializers.ModelSerializer):
    class Meta:
        model = Applicant
        exclude = ['date_of_birth']

    def validate_id_number(self, value):
        try:
            self._born = parse_sa_id(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e))
        return value.strip()

    def create(self, validated):
        return Applicant.objects.create(date_of_birth=self._born, **validated)


class ApplicationIn(serializers.ModelSerializer):
    applicant_id = serializers.PrimaryKeyRelatedField(queryset=Applicant.objects.all(), source='applicant')

    class Meta:
        model = LoanApplication
        fields = ['applicant_id'] + services.EDITABLE_FIELDS

    def validate(self, data):
        if data.get('net_monthly_income', 0) < 0 or data.get('declared_living_expenses', 0) < 0:
            raise serializers.ValidationError('amounts cannot be negative')
        return data


class AssessmentOut(serializers.ModelSerializer):
    class Meta:
        model = Assessment
        exclude = ['id', 'application']


class DocumentOut(serializers.ModelSerializer):
    verified_by = serializers.CharField(source='verified_by.username', default=None)

    class Meta:
        model = Document
        fields = ['id', 'kind', 'file_name', 'status', 'verified_by', 'note', 'uploaded_at']


class ApplicationOut(serializers.ModelSerializer):
    applicant = serializers.SerializerMethodField()
    documents = DocumentOut(many=True, read_only=True)
    latest_assessment = serializers.SerializerMethodField()

    class Meta:
        model = LoanApplication
        fields = ['reference', 'status', 'applicant'] + services.EDITABLE_FIELDS + [
            'decision_reason', 'offered_amount', 'offered_annual_rate', 'offered_instalment', 'documents',
            'latest_assessment', 'created_at', 'updated_at']

    def get_applicant(self, app):
        a = app.applicant
        # staff screens don't need the full ID number; mask it everywhere except where it's captured
        return {'id': a.id, 'name': str(a), 'id_number': a.id_number[:6] + '*******',
                'employment_status': a.employment_status}

    def get_latest_assessment(self, app):
        latest = app.assessments.first()
        return AssessmentOut(latest).data if latest else None


class HistoryOut(serializers.ModelSerializer):
    actor = serializers.CharField(source='actor.username', default='system')

    class Meta:
        model = AuditEntry
        fields = ['at', 'actor', 'action', 'changes', 'note']


def _staff(request):
    if not roles(request.user) & STAFF:
        raise NotAllowed('staff only')


def _handle(fn):
    """Maps workflow exceptions onto HTTP status codes in one place."""
    def wrapper(request, *args, **kwargs):
        try:
            _staff(request)
            return fn(request, *args, **kwargs)
        except NotAllowed as e:
            return Response({'detail': str(e)}, status=403)
        except WorkflowError as e:
            return Response({'detail': str(e)}, status=409)
    wrapper.__name__ = fn.__name__
    return wrapper


def _needs(request, *role_names):
    if not roles(request.user) & (set(role_names) | {'superuser'}):
        raise NotAllowed('requires one of: %s' % ', '.join(role_names))


@api_view(['POST'])
@_handle
def create_applicant(request):
    _needs(request, 'consultant')
    s = ApplicantIn(data=request.data)
    s.is_valid(raise_exception=True)
    a = s.save()
    return Response({'id': a.id, 'date_of_birth': a.date_of_birth}, status=201)


@api_view(['GET', 'POST'])
@_handle
def applications(request):
    if request.method == 'POST':
        _needs(request, 'consultant')
        s = ApplicationIn(data=request.data)
        s.is_valid(raise_exception=True)
        data = dict(s.validated_data)
        app = services.create(data.pop('applicant'), data, request.user)
        return Response(ApplicationOut(app).data, status=201)
    qs = LoanApplication.objects.select_related('applicant').prefetch_related('documents', 'assessments')
    if status := request.query_params.get('status'):
        qs = qs.filter(status=status)
    return Response(ApplicationOut(qs[:100], many=True).data)


@api_view(['GET', 'PATCH'])
@_handle
def application(request, ref):
    app = get_object_or_404(LoanApplication, reference=ref)
    if request.method == 'PATCH':
        _needs(request, 'consultant')
        s = ApplicationIn(app, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        services.update(app, s.validated_data, request.user)
    return Response(ApplicationOut(app).data)


ACTIONS = {
    'submit': (('consultant',), lambda app, r: services.submit(app, r.user)),
    'assess': ((), lambda app, r: services.assess(app, r.user)),
    'approve': ((), lambda app, r: services.approve(app, r.user, r.data.get('amount'), r.data.get('note', ''))),
    'decline': ((), lambda app, r: services.decline(app, r.user, r.data.get('reason', ''))),
    'accept': (('consultant',), lambda app, r: services.accept_offer(app, r.user)),
    'disburse': ((), lambda app, r: services.disburse(app, r.user)),
    'withdraw': (('consultant',), lambda app, r: services.withdraw(app, r.user, r.data.get('reason', ''))),
}


@api_view(['POST'])
@_handle
def action(request, ref, name):
    if name not in ACTIONS:
        return Response({'detail': 'unknown action'}, status=404)
    needed, fn = ACTIONS[name]
    if needed:
        _needs(request, *needed)
    app = get_object_or_404(LoanApplication, reference=ref)
    fn(app, request)
    app.refresh_from_db()
    return Response(ApplicationOut(app).data)


@api_view(['POST'])
@_handle
def upload_document(request, ref):
    _needs(request, 'consultant')
    app = get_object_or_404(LoanApplication, reference=ref)
    upload = request.FILES.get('file')
    kind = request.data.get('kind')
    if upload is None or kind not in Document.Kind.values:
        return Response({'detail': 'send a file and a kind (%s)' % ', '.join(Document.Kind.values)}, status=400)
    if upload.size > 10 * 1024 * 1024:
        return Response({'detail': 'file is larger than 10 MB'}, status=413)
    digest = hashlib.sha256(upload.read()).hexdigest()
    # file storage (S3 in production) is out of scope here; the hash ties the record to the exact file reviewed
    doc = services.add_document(app, kind, upload.name, digest, request.user)
    return Response(DocumentOut(doc).data, status=201)


@api_view(['POST'])
@_handle
def review_document(request, pk):
    doc = get_object_or_404(Document, pk=pk)
    verified = request.data.get('verified')
    if not isinstance(verified, bool):
        return Response({'detail': 'verified must be true or false'}, status=400)
    services.review_document(doc, request.user, verified, request.data.get('note', ''))
    return Response(DocumentOut(doc).data)


@api_view(['GET'])
@_handle
def history(request, ref):
    app = get_object_or_404(LoanApplication, reference=ref)
    return Response(HistoryOut(app.history.select_related('actor'), many=True).data)
