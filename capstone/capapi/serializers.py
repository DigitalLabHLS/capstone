import logging

from rest_framework import serializers

from capdb import models
from .models import APIUser
from .resources import email

logger = logging.getLogger(__name__)


class CaseSerializer(serializers.ModelSerializer):
    jurisdiction = serializers.ReadOnlyField(source='jurisdiction.slug')
    jurisdiction_name = serializers.ReadOnlyField(source='jurisdiction.name')
    jurisdiction_id = serializers.ReadOnlyField(source='jurisdiction.id')
    court_name = serializers.ReadOnlyField(source='court.name')
    court_id = serializers.ReadOnlyField(source='court.id')
    reporter_name = serializers.ReadOnlyField(source='reporter.name')
    reporter_id = serializers.ReadOnlyField(source='reporter.id')
    reporter_abbreviation = serializers.ReadOnlyField(source='reporter.name_abbreviation')
    citation = serializers.ReadOnlyField(source='citation.cite')
    citation_slug = serializers.ReadOnlyField(source='citation.slug')

    class Meta:
        model = models.CaseMetadata
        fields = '__all__'


class CitationSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Citation
        fields = ('slug', 'type', 'cite')


class JurisdictionSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Jurisdiction
        fields = '__all__'


class VolumeSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.VolumeMetadata
        fields = '__all__'


class ReporterSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Reporter
        fields = '__all__'


class CourtSerializer(serializers.ModelSerializer):
    jurisdiction = serializers.HyperlinkedRelatedField(view_name='jurisdiction-detail', read_only=True)
    jurisdiction_id = serializers.ReadOnlyField(source='jurisdiction.id')
    jurisdiction_name = serializers.ReadOnlyField(source='jurisdiction.name')

    class Meta:
        model = models.Court
        lookup_field = 'id'
        fields = ('id', 'name', 'name_abbreviation', 'jurisdiction', 'jurisdiction_id', 'jurisdiction_name')
        extra_kwargs = {
            'url': {'lookup_field': 'id'}
        }


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = APIUser
        fields = '__all__'
        read_only_fields = ('is_admin', 'is_researcher', 'activation_key', 'is_validated', 'case_allowance', 'key_expires')
        lookup_field = 'email'

    def verify_with_nonce(self, user_id, activation_nonce):
        found_user = APIUser.objects.get(pk=user_id)
        if not found_user.is_authenticated():
            found_user.authenticate_user(activation_nonce=activation_nonce)
        return found_user


class RegisterUserSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    first_name = serializers.CharField(required=False)
    last_name = serializers.CharField(required=False)
    password = serializers.CharField(style={'input_type': 'password'})
    confirm_password = serializers.CharField(style={'input_type': 'password'})

    class Meta:
        # write_only_fields = ('password', 'confirm_password')
        fields = '__all__'

    def validate_email(self, email):
        existing = APIUser.objects.filter(email=email).first()
        if existing:
            msg = "Someone with that email address has already registered. Was it you?"
            raise serializers.ValidationError(msg)

        return email

    def validate(self, data):
        if not data.get('password') or not data.get('confirm_password'):
            msg = "Please enter a password and confirm it."
            raise serializers.ValidationError(msg)

        if data.get('password') != data.get('confirm_password'):
            raise serializers.ValidationError("Those passwords don't match.")

        return data

    def create(self, validated_data):
        try:
            user = APIUser.objects.create_user(**validated_data)
            email(reason='new_signup', user=user)
            return user
        except Exception as e:
            raise Exception(e)


class LoginSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(max_length=100, style={'input_type': 'password'})

    class Meta:
        model = APIUser
        fields = ('email', 'password')
        write_only_fields = ('password')
        lookup_field = 'email'

    def verify_with_password(self, email, password):
        user = APIUser.objects.get(email=email)
        correct_password = user.check_password(password)
        if not correct_password:
            raise serializers.ValidationError('Invalid password')
        return user
