from __future__ import unicode_literals

import json
import os
import datetime
from django.db import models,transaction
from django.dispatch import receiver
from django.db.models.signals import pre_delete
from django.utils.encoding import python_2_unicode_compatible
from django.core.exceptions import ValidationError
from django.contrib.postgres.fields.jsonb import JSONField
from django.utils import timezone
from django.contrib.sites.models import Site
from taggit.managers import TaggableManager
from taggit.models import TaggedItemBase
from ledger.accounts.models import Organisation as ledger_organisation
from ledger.accounts.models import EmailUser, RevisionedMixin
from ledger.licence.models import  Licence
from commercialoperator import exceptions
from commercialoperator.components.organisations.models import Organisation
from commercialoperator.components.main.models import CommunicationsLogEntry, UserAction, Document, Region, District, Tenure, ApplicationType, Park, Activity, ActivityCategory, AccessType, Trail, Section, Zone, RequiredDocument
from commercialoperator.components.main.utils import get_department_user
from commercialoperator.components.proposals.email import send_referral_email_notification, send_proposal_decline_email_notification,send_proposal_approval_email_notification, send_amendment_email_notification
from commercialoperator.ordered_model import OrderedModel
from commercialoperator.components.proposals.email import send_submit_email_notification, send_external_submit_email_notification, send_approver_decline_email_notification, send_approver_approve_email_notification, send_referral_complete_email_notification, send_proposal_approver_sendback_email_notification, send_qaofficer_email_notification, send_qaofficer_complete_email_notification
import copy
import subprocess
from django.db.models import Q

import logging
logger = logging.getLogger(__name__)


def update_proposal_doc_filename(instance, filename):
    return 'proposals/{}/documents/{}'.format(instance.proposal.id,filename)

def update_onhold_doc_filename(instance, filename):
    return 'proposals/{}/on_hold/{}'.format(instance.proposal.id,filename)

def update_qaofficer_doc_filename(instance, filename):
    return 'proposals/{}/qaofficer/{}'.format(instance.proposal.id,filename)

def update_referral_doc_filename(instance, filename):
    return 'proposals/{}/referral/{}/documents/{}'.format(instance.referral.proposal.id,instance.referral.id,filename)

def update_proposal_required_doc_filename(instance, filename):
    return 'proposals/{}/required_documents/{}/{}'.format(instance.proposal.id,instance.required_doc.id,filename)

def update_proposal_comms_log_filename(instance, filename):
    return 'proposals/{}/communications/{}/{}'.format(instance.log_entry.proposal.id,instance.id,filename)

def application_type_choicelist():
    try:
        return [( (choice.name), (choice.name) ) for choice in ApplicationType.objects.filter(visible=True)]
    except:
        # required because on first DB tables creation, there are no ApplicationType objects -- setting a default value
        return ( ('T Class', 'T Class'), )

class ProposalType(models.Model):
    #name = models.CharField(verbose_name='Application name (eg. commercialoperator, Apiary)', max_length=24)
    #application_type = models.ForeignKey(ApplicationType, related_name='aplication_types')
    description = models.CharField(max_length=256, blank=True, null=True)
    #name = models.CharField(verbose_name='Application name (eg. commercialoperator, Apiary)', max_length=24, choices=application_type_choicelist(), default=application_type_choicelist()[0][0])
    name = models.CharField(verbose_name='Application name (eg. T Class, Filming, Event)', max_length=64, choices=application_type_choicelist(), default='T Class')
    schema = JSONField(default=[{}])
    #activities = TaggableManager(verbose_name="Activities",help_text="A comma-separated list of activities.")
    #site = models.OneToOneField(Site, default='1')
    replaced_by = models.ForeignKey('self', on_delete=models.PROTECT, blank=True, null=True)
    version = models.SmallIntegerField(default=1, blank=False, null=False)

    def __str__(self):
        return '{} - v{}'.format(self.name, self.version)

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('name', 'version')


class TaggedProposalAssessorGroupRegions(TaggedItemBase):
    content_object = models.ForeignKey("ProposalAssessorGroup")

    class Meta:
        app_label = 'commercialoperator'

class TaggedProposalAssessorGroupActivities(TaggedItemBase):
    content_object = models.ForeignKey("ProposalAssessorGroup")

    class Meta:
        app_label = 'commercialoperator'

class ProposalAssessorGroup(models.Model):
    name = models.CharField(max_length=255)
    members = models.ManyToManyField(EmailUser)
    region = models.ForeignKey(Region, null=True, blank=True)
    default = models.BooleanField(default=False)

    class Meta:
        app_label = 'commercialoperator'

    def __str__(self):
        return self.name

    def clean(self):
        try:
            default = ProposalAssessorGroup.objects.get(default=True)
        except ProposalAssessorGroup.DoesNotExist:
            default = None

        if self.pk:
            if not self.default and not self.region:
                raise ValidationError('Only default can have no region set for proposal assessor group. Please specifiy region')
#            elif default and not self.default:
#                raise ValidationError('There can only be one default proposal assessor group')
        else:
            if default and self.default:
                raise ValidationError('There can only be one default proposal assessor group')

    def member_is_assigned(self,member):
        for p in self.current_proposals:
            if p.assigned_officer == member:
                return True
        return False

    @property
    def current_proposals(self):
        assessable_states = ['with_assessor','with_referral','with_assessor_requirements']
        return Proposal.objects.filter(processing_status__in=assessable_states)

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]

class TaggedProposalApproverGroupRegions(TaggedItemBase):
    content_object = models.ForeignKey("ProposalApproverGroup")

    class Meta:
        app_label = 'commercialoperator'

class TaggedProposalApproverGroupActivities(TaggedItemBase):
    content_object = models.ForeignKey("ProposalApproverGroup")

    class Meta:
        app_label = 'commercialoperator'

class ProposalApproverGroup(models.Model):
    name = models.CharField(max_length=255)
    #members = models.ManyToManyField(EmailUser,blank=True)
    #regions = TaggableManager(verbose_name="Regions",help_text="A comma-separated list of regions.",through=TaggedProposalApproverGroupRegions,related_name = "+",blank=True)
    #activities = TaggableManager(verbose_name="Activities",help_text="A comma-separated list of activities.",through=TaggedProposalApproverGroupActivities,related_name = "+",blank=True)
    members = models.ManyToManyField(EmailUser)
    region = models.ForeignKey(Region, null=True, blank=True)
    default = models.BooleanField(default=False)

    class Meta:
        app_label = 'commercialoperator'

    def __str__(self):
        return self.name

    def clean(self):
        try:
            default = ProposalApproverGroup.objects.get(default=True)
        except ProposalApproverGroup.DoesNotExist:
            default = None

        if self.pk:
            if not self.default and not self.region:
                raise ValidationError('Only default can have no region set for proposal assessor group. Please specifiy region')

#            if int(self.pk) != int(default.id):
#                if default and self.default:
#                    raise ValidationError('There can only be one default proposal approver group')
        else:
            if default and self.default:
                raise ValidationError('There can only be one default proposal approver group')

    def member_is_assigned(self,member):
        for p in self.current_proposals:
            if p.assigned_approver == member:
                return True
        return False

    @property
    def current_proposals(self):
        assessable_states = ['with_approver']
        return Proposal.objects.filter(processing_status__in=assessable_states)

    @property
    def members_email(self):
        return [i.email for i in self.members.all()]

class ProposalDocument(Document):
    proposal = models.ForeignKey('Proposal',related_name='documents')
    _file = models.FileField(upload_to=update_proposal_doc_filename)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(ProposalDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'commercialoperator'

class OnHoldDocument(Document):
    proposal = models.ForeignKey('Proposal',related_name='onhold_documents')
    _file = models.FileField(upload_to=update_onhold_doc_filename)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    def delete(self):
        if self.can_delete:
            return super(ProposalDocument, self).delete()

#Documents on Activities(land)and Activities(Marine) tab for T-Class related to required document questions
class ProposalRequiredDocument(Document):
    proposal = models.ForeignKey('Proposal',related_name='required_documents')
    _file = models.FileField(upload_to=update_proposal_required_doc_filename)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    required_doc = models.ForeignKey('RequiredDocument',related_name='proposals')

    def delete(self):
        if self.can_delete:
            return super(ProposalRequiredDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'commercialoperator'

class QAOfficerDocument(Document):
    proposal = models.ForeignKey('Proposal',related_name='qaofficer_documents')
    _file = models.FileField(upload_to=update_qaofficer_doc_filename)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted
    visible = models.BooleanField(default=True) # to prevent deletion on file system, hidden and still be available in history

    def delete(self):
        if self.can_delete:
            return super(QAOfficerDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'commercialoperator'


class ReferralDocument(Document):
    referral = models.ForeignKey('Referral',related_name='referral_documents')
    _file = models.FileField(upload_to=update_referral_doc_filename)
    input_name = models.CharField(max_length=255,null=True,blank=True)
    can_delete = models.BooleanField(default=True) # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(ProposalDocument, self).delete()
        logger.info('Cannot delete existing document object after Proposal has been submitted (including document submitted before Proposal pushback to status Draft): {}'.format(self.name))

    class Meta:
        app_label = 'commercialoperator'

class ProposalApplicantDetails(models.Model):
    first_name = models.CharField(max_length=24, blank=True, default='')

    class Meta:
        app_label = 'commercialoperator'


class ProposalActivitiesLand(models.Model):
    activities_land = models.CharField(max_length=24, blank=True, default='')

    class Meta:
        app_label = 'commercialoperator'


class ProposalActivitiesMarine(models.Model):
    activities_marine = models.CharField(max_length=24, blank=True, default='')

    class Meta:
        app_label = 'commercialoperator'


class Proposal(RevisionedMixin):
#class Proposal(models.Model):

    CUSTOMER_STATUS_CHOICES = (('temp', 'Temporary'), ('draft', 'Draft'),
                               ('with_assessor', 'Under Review'),
                               ('amendment_required', 'Amendment Required'),
                               ('approved', 'Approved'),
                               ('declined', 'Declined'),
                               ('discarded', 'Discarded'),
                               )

    # List of statuses from above that allow a customer to edit an application.
    CUSTOMER_EDITABLE_STATE = ['temp',
                                'draft',
                                'amendment_required',
                            ]

    # List of statuses from above that allow a customer to view an application (read-only)
    CUSTOMER_VIEWABLE_STATE = ['with_assessor', 'under_review', 'id_required', 'returns_required', 'approved', 'declined']

    PROCESSING_STATUS_TEMP = 'temp'
    PROCESSING_STATUS_DRAFT = 'draft'
    PROCESSING_STATUS_WITH_ASSESSOR = 'with_assessor'
    PROCESSING_STATUS_ONHOLD = 'on_hold'
    PROCESSING_STATUS_WITH_QA_OFFICER = 'with_qa_officer'
    PROCESSING_STATUS_WITH_REFERRAL = 'with_referral'
    PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS = 'with_assessor_requirements'
    PROCESSING_STATUS_WITH_APPROVER = 'with_approver'
    PROCESSING_STATUS_RENEWAL = 'renewal'
    PROCESSING_STATUS_LICENCE_AMENDMENT = 'licence_amendment'
    PROCESSING_STATUS_AWAITING_APPLICANT_RESPONSE = 'awaiting_applicant_respone'
    PROCESSING_STATUS_AWAITING_ASSESSOR_RESPONSE = 'awaiting_assessor_response'
    PROCESSING_STATUS_AWAITING_RESPONSES = 'awaiting_responses'
    PROCESSING_STATUS_READY_FOR_CONDITIONS = 'ready_for_conditions'
    PROCESSING_STATUS_READY_TO_ISSUE = 'ready_to_issue'
    PROCESSING_STATUS_APPROVED = 'approved'
    PROCESSING_STATUS_DECLINED = 'declined'
    PROCESSING_STATUS_DISCARDED = 'discarded'
    PROCESSING_STATUS_CHOICES = ((PROCESSING_STATUS_TEMP, 'Temporary'),
                                 (PROCESSING_STATUS_DRAFT, 'Draft'),
                                 (PROCESSING_STATUS_WITH_ASSESSOR, 'With Assessor'),
                                 (PROCESSING_STATUS_ONHOLD, 'On Hold'),
                                 (PROCESSING_STATUS_WITH_QA_OFFICER, 'With QA Officer'),
                                 (PROCESSING_STATUS_WITH_REFERRAL, 'With Referral'),
                                 (PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS, 'With Assessor (Requirements)'),
                                 (PROCESSING_STATUS_WITH_APPROVER, 'With Approver'),
                                 (PROCESSING_STATUS_RENEWAL, 'Renewal'),
                                 (PROCESSING_STATUS_LICENCE_AMENDMENT, 'Licence Amendment'),
                                 (PROCESSING_STATUS_AWAITING_APPLICANT_RESPONSE, 'Awaiting Applicant Response'),
                                 (PROCESSING_STATUS_AWAITING_ASSESSOR_RESPONSE, 'Awaiting Assessor Response'),
                                 (PROCESSING_STATUS_AWAITING_RESPONSES, 'Awaiting Responses'),
                                 (PROCESSING_STATUS_READY_FOR_CONDITIONS, 'Ready for Conditions'),
                                 (PROCESSING_STATUS_READY_TO_ISSUE, 'Ready to Issue'),
                                 (PROCESSING_STATUS_APPROVED, 'Approved'),
                                 (PROCESSING_STATUS_DECLINED, 'Declined'),
                                 (PROCESSING_STATUS_DISCARDED, 'Discarded'),
                                 )

    ID_CHECK_STATUS_CHOICES = (('not_checked', 'Not Checked'), ('awaiting_update', 'Awaiting Update'),
                               ('updated', 'Updated'), ('accepted', 'Accepted'))

    COMPLIANCE_CHECK_STATUS_CHOICES = (
        ('not_checked', 'Not Checked'), ('awaiting_returns', 'Awaiting Returns'), ('completed', 'Completed'),
        ('accepted', 'Accepted'))

    CHARACTER_CHECK_STATUS_CHOICES = (
        ('not_checked', 'Not Checked'), ('accepted', 'Accepted'))

    REVIEW_STATUS_CHOICES = (
        ('not_reviewed', 'Not Reviewed'), ('awaiting_amendments', 'Awaiting Amendments'), ('amended', 'Amended'),
        ('accepted', 'Accepted'))

#    PROPOSAL_STATE_NEW_LICENCE = 'New Licence'
#    PROPOSAL_STATE_AMENDMENT = 'Amendment'
#    PROPOSAL_STATE_RENEWAL = 'Renewal'
#    PROPOSAL_STATE_CHOICES = (
#        (1, PROPOSAL_STATE_NEW_LICENCE),
#        (2, PROPOSAL_STATE_AMENDMENT),
#        (3, PROPOSAL_STATE_RENEWAL),
#    )

    APPLICATION_TYPE_CHOICES = (
        ('new_proposal', 'New Proposal'),
        ('amendment', 'Amendment'),
        ('renewal', 'Renewal'),
    )

    proposal_type = models.CharField('Proposal Type', max_length=40, choices=APPLICATION_TYPE_CHOICES,
                                        default=APPLICATION_TYPE_CHOICES[0][0])
    #proposal_state = models.PositiveSmallIntegerField('Proposal state', choices=PROPOSAL_STATE_CHOICES, default=1)

    data = JSONField(blank=True, null=True)
    assessor_data = JSONField(blank=True, null=True)
    comment_data = JSONField(blank=True, null=True)
    schema = JSONField(blank=False, null=False)
    proposed_issuance_approval = JSONField(blank=True, null=True)
    #hard_copy = models.ForeignKey(Document, blank=True, null=True, related_name='hard_copy')

    customer_status = models.CharField('Customer Status', max_length=40, choices=CUSTOMER_STATUS_CHOICES,
                                       default=CUSTOMER_STATUS_CHOICES[1][0])
    applicant = models.ForeignKey(Organisation, blank=True, null=True, related_name='proposals')

    lodgement_number = models.CharField(max_length=9, blank=True, default='')
    lodgement_sequence = models.IntegerField(blank=True, default=0)
    #lodgement_date = models.DateField(blank=True, null=True)
    lodgement_date = models.DateTimeField(blank=True, null=True)

    proxy_applicant = models.ForeignKey(EmailUser, blank=True, null=True, related_name='commercialoperator_proxy')
    submitter = models.ForeignKey(EmailUser, blank=True, null=True, related_name='commercialoperator_proposals')

    assigned_officer = models.ForeignKey(EmailUser, blank=True, null=True, related_name='commercialoperator_proposals_assigned', on_delete=models.SET_NULL)
    assigned_approver = models.ForeignKey(EmailUser, blank=True, null=True, related_name='commercialoperator_proposals_approvals', on_delete=models.SET_NULL)
    processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
                                         default=PROCESSING_STATUS_CHOICES[1][0])
    prev_processing_status = models.CharField(max_length=30, blank=True, null=True)
    id_check_status = models.CharField('Identification Check Status', max_length=30, choices=ID_CHECK_STATUS_CHOICES,
                                       default=ID_CHECK_STATUS_CHOICES[0][0])
    compliance_check_status = models.CharField('Return Check Status', max_length=30, choices=COMPLIANCE_CHECK_STATUS_CHOICES,
                                            default=COMPLIANCE_CHECK_STATUS_CHOICES[0][0])
    character_check_status = models.CharField('Character Check Status', max_length=30,
                                              choices=CHARACTER_CHECK_STATUS_CHOICES,
                                              default=CHARACTER_CHECK_STATUS_CHOICES[0][0])
    review_status = models.CharField('Review Status', max_length=30, choices=REVIEW_STATUS_CHOICES,
                                     default=REVIEW_STATUS_CHOICES[0][0])

    approval = models.ForeignKey('commercialoperator.Approval',null=True,blank=True)

    previous_application = models.ForeignKey('self', on_delete=models.PROTECT, blank=True, null=True)
    proposed_decline_status = models.BooleanField(default=False)
    #qaofficer_referral = models.BooleanField(default=False)
    #qaofficer_referral = models.OneToOneField('QAOfficerReferral', blank=True, null=True)
    # Special Fields
    title = models.CharField(max_length=255,null=True,blank=True)
    activity = models.CharField(max_length=255,null=True,blank=True)
    #region = models.CharField(max_length=255,null=True,blank=True)
    tenure = models.CharField(max_length=255,null=True,blank=True)
    #activity = models.ForeignKey(Activity, null=True, blank=True)
    region = models.ForeignKey(Region, null=True, blank=True)
    district = models.ForeignKey(District, null=True, blank=True)
    #tenure = models.ForeignKey(Tenure, null=True, blank=True)
    application_type = models.ForeignKey(ApplicationType)
    approval_level = models.CharField('Activity matrix approval level', max_length=255,null=True,blank=True)
    approval_level_document = models.ForeignKey(ProposalDocument, blank=True, null=True, related_name='approval_level_document')
    approval_comment = models.TextField(blank=True)

    # common
    applicant_details = models.OneToOneField(ProposalApplicantDetails, blank=True, null=True) #, related_name='applicant_details')
    training_completed = models.BooleanField(default=False)
    #payment = models.OneToOneField(ProposalPayment, blank=True, null=True)
    #confirmation = models.OneToOneField(ProposalConfirmation, blank=True, null=True)

    # T Class
    activities_land = models.OneToOneField(ProposalActivitiesLand, blank=True, null=True) #, related_name='activities_land')
    activities_marine = models.OneToOneField(ProposalActivitiesMarine, blank=True, null=True) #, related_name='activities_marine')
    #other_details = models.OneToOneField(ProposalOtherDetails, blank=True, null=True, related_name='proposal')
    #online_training = models.OneToOneField(ProposalOnlineTraining, blank=True, null=True)

    # Filming
    #activity = models.OneToOneField(ProposalActivity, blank=True, null=True)
    #access = models.OneToOneField(ProposalAccess, blank=True, null=True)
    #equipment = models.OneToOneField(ProposalEquipment, blank=True, null=True)
    #other_details = models.OneToOneField(ProposalOtherDetails, blank=True, null=True)
    #online_training = models.OneToOneField(ProposalOnlineTraining, blank=True, null=True)

    # Event
    #activities = models.OneToOneField(ProposalActivities, blank=True, null=True)
    #event_management = models.OneToOneField(ProposalEventManagement, blank=True, null=True)
    #vehicles_vessels = models.OneToOneField(ProposalVehiclesVessels, blank=True, null=True)
    #other_details = models.OneToOneField(ProposalOtherDetails, blank=True, null=True)
    #online_training = models.OneToOneField(ProposalOnlineTraining, blank=True, null=True)


    class Meta:
        app_label = 'commercialoperator'

    def __str__(self):
        return str(self.id)

    #Append 'P' to Proposal id to generate Lodgement number. Lodgement number and lodgement sequence are used to generate Reference.
    def save(self, *args, **kwargs):
        super(Proposal, self).save(*args,**kwargs)
        if self.lodgement_number == '':
            new_lodgment_id = 'P{0:06d}'.format(self.pk)
            self.lodgement_number = new_lodgment_id
            self.save()

    @property
    def reference(self):
        return '{}-{}'.format(self.lodgement_number, self.lodgement_sequence)

    def qa_officers(self, name=None):
        if not name:
            return QAOfficerGroup.objects.get(default=True).members.all().values_list('email', flat=True)
        else:
            return QAOfficerGroup.objects.get(name=name).members.all().values_list('email', flat=True)

    @property
    def get_history(self):
        """ Return the prev proposal versions """
        l = []
        p = copy.deepcopy(self)
        while (p.previous_application):
            l.append( dict(id=p.previous_application.id, modified=p.previous_application.modified_date) )
            p = p.previous_application
        return l


    def _get_history(self):
        """ Return the prev proposal versions """
        l = []
        p = copy.deepcopy(self)
        while (p.previous_application):
            l.append( [p.id, p.previous_application.id] )
            p = p.previous_application
        return l

    @property
    def is_assigned(self):
        return self.assigned_officer is not None

    @property
    def is_temporary(self):
        return self.customer_status == 'temp' and self.processing_status == 'temp'

    @property
    def can_user_edit(self):
        """
        :return: True if the application is in one of the editable status.
        """
        return self.customer_status in self.CUSTOMER_EDITABLE_STATE

    @property
    def can_user_view(self):
        """
        :return: True if the application is in one of the approved status.
        """
        return self.customer_status in self.CUSTOMER_VIEWABLE_STATE



    @property
    def is_discardable(self):
        """
        An application can be discarded by a customer if:
        1 - It is a draft
        2- or if the application has been pushed back to the user
        """
        return self.customer_status == 'draft' or self.processing_status == 'awaiting_applicant_response'

    @property
    def is_deletable(self):
        """
        An application can be deleted only if it is a draft and it hasn't been lodged yet
        :return:
        """
        return self.customer_status == 'draft' and not self.lodgement_number

    @property
    def latest_referrals(self):
        return self.referrals.all()[:2]

    @property
    def land_parks(self):
        return self.parks.filter(park__park_type='land')

    @property
    def marine_parks(self):
        return self.parks.filter(park__park_type='marine')

    @property
    def regions_list(self):
        #return self.region.split(',') if self.region else []
        return [self.region.name] if self.region else []

    @property
    def permit(self):
        return self.approval.licence_document._file.url if self.approval else None

    @property
    def allowed_assessors(self):
        if self.processing_status == 'with_approver':
            group = self.__approver_group()
        else:
            group = self.__assessor_group()
        return group.members.all() if group else []

    @property
    def compliance_assessors(self):
        group = self.__assessor_group()
        return group.members.all() if group else []

    @property
    def can_officer_process(self):
        """
        :return: True if the application is in one of the processable status for Assessor role.
        """
        officer_view_state = ['draft','approved','declined','temp','discarded']
        if self.processing_status in officer_view_state:
            return False
        else:
            return True

    @property
    def amendment_requests(self):
        qs =AmendmentRequest.objects.filter(proposal = self)
        return qs

    def __assessor_group(self):
        # TODO get list of assessor groups based on region and activity
        if self.region and self.activity:
            try:
                check_group = ProposalAssessorGroup.objects.filter(
                    #activities__name__in=[self.activity],
                    region__name__in=self.regions_list
                ).distinct()
                if check_group:
                    return check_group[0]
            except ProposalAssessorGroup.DoesNotExist:
                pass
        default_group = ProposalAssessorGroup.objects.get(default=True)

        return default_group


    def __approver_group(self):
        # TODO get list of approver groups based on region and activity
        if self.region and self.activity:
            try:
                check_group = ProposalApproverGroup.objects.filter(
                    #activities__name__in=[self.activity],
                    region__name__in=self.regions_list
                ).distinct()
                if check_group:
                    return check_group[0]
            except ProposalApproverGroup.DoesNotExist:
                pass
        default_group = ProposalApproverGroup.objects.get(default=True)

        return default_group

    def __check_proposal_filled_out(self):
        if not self.data:
            raise exceptions.ProposalNotComplete()
        missing_fields = []
        required_fields = {
        #    'region':'Region/District',
        #    'title': 'Title',
        #    'activity': 'Activity'
        }
        #import ipdb; ipdb.set_trace()
        for k,v in required_fields.items():
            val = getattr(self,k)
            if not val:
                missing_fields.append(v)
        return missing_fields

    @property
    def assessor_recipients(self):
        recipients = []
        #import ipdb; ipdb.set_trace()
        try:
            recipients = ProposalAssessorGroup.objects.get(region=self.region).members_email
        except:
            recipients = ProposalAssessorGroup.objects.get(default=True).members_email

        #if self.submitter.email not in recipients:
        #    recipients.append(self.submitter.email)
        return recipients

    @property
    def approver_recipients(self):
        recipients = []
        try:
            recipients = ProposalApproverGroup.objects.get(region=self.region).members_email
        except:
            recipients = ProposalApproverGroup.objects.get(default=True).members_email

        #if self.submitter.email not in recipients:
        #    recipients.append(self.submitter.email)
        return recipients


    def can_assess(self,user):
        #if self.processing_status == 'on_hold' or self.processing_status == 'with_assessor' or self.processing_status == 'with_referral' or self.processing_status == 'with_assessor_requirements':
        if self.processing_status in ['on_hold', 'with_qa_officer', 'with_assessor', 'with_referral', 'with_assessor_requirements']:
            return self.__assessor_group() in user.proposalassessorgroup_set.all()
        elif self.processing_status == 'with_approver':
            return self.__approver_group() in user.proposalapprovergroup_set.all()
        else:
            return False

    def assessor_comments_view(self,user):

        if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral' or self.processing_status == 'with_assessor_requirements' or self.processing_status == 'with_approver':
            try:
                referral = Referral.objects.get(proposal=self,referral=user)
            except:
                referral = None
            if referral:
                return True
            elif self.__assessor_group() in user.proposalassessorgroup_set.all():
                return True
            elif self.__approver_group() in user.proposalapprovergroup_set.all():
                return True
            else:
                return False
        else:
            return False

    def has_assessor_mode(self,user):
        status_without_assessor = ['with_approver','approved','declined','draft']
        if self.processing_status in status_without_assessor:
            return False
        else:
            if self.assigned_officer:
                if self.assigned_officer == user:
                    return self.__assessor_group() in user.proposalassessorgroup_set.all()
                else:
                    return False
            else:
                return self.__assessor_group() in user.proposalassessorgroup_set.all()

    def log_user_action(self, action, request):
        return ProposalUserAction.log_action(self, action, request.user)

    def submit(self,request,viewset):
        from commercialoperator.components.proposals.utils import save_proponent_data
        with transaction.atomic():
            if self.can_user_edit:
                # Save the data first
                save_proponent_data(self,request,viewset)
                # Check if the special fields have been completed
                missing_fields = self.__check_proposal_filled_out()
                if missing_fields:
                    error_text = 'The proposal has these missing fields, {}'.format(','.join(missing_fields))
                    raise exceptions.ProposalMissingFields(detail=error_text)
                self.submitter = request.user
                #self.lodgement_date = datetime.datetime.strptime(timezone.now().strftime('%Y-%m-%d'),'%Y-%m-%d').date()
                self.lodgement_date = timezone.now()
                if (self.amendment_requests):
                    qs = self.amendment_requests.filter(status = "requested")
                    if (qs):
                        for q in qs:
                            q.status = 'amended'
                            q.save()

                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(self.id),request)
                # Create a log entry for the organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(self.id),request)

                #import ipdb; ipdb.set_trace()
                ret1 = send_submit_email_notification(request, self)
                ret2 = send_external_submit_email_notification(request, self)

                #import ipdb; ipdb.set_trace()
                #self.save_form_tabs(request)
                if ret1 and ret2:
                    self.processing_status = 'with_assessor'
                    self.customer_status = 'with_assessor'
                    self.documents.all().update(can_delete=False)
                    self.save()
                else:
                    raise ValidationError('An error occurred while submitting proposal (Submit email notifications failed)')
            else:
                raise ValidationError('You can\'t edit this proposal at this moment')

    def save_form_tabs(self,request):
        self.applicant_details = ProposalApplicantDetails.objects.create(first_name=request.data['first_name'])
        self.activities_land = ProposalActivitiesLand.objects.create(activities_land=request.data['activities_land'])
        self.activities_marine = ProposalActivitiesMarine.objects.create(activities_marine=request.data['activities_marine'])
        #self.save()

    def save_parks(self,request,parks):
        with transaction.atomic():
            if parks:
                try:
                    current_parks=self.parks.all()
                    if current_parks:
                        #print current_parks
                        for p in current_parks:
                            p.delete()
                    for item in parks:
                        try:
                            park=Park.objects.get(id=item)
                            ProposalPark.objects.create(proposal=self, park=park)
                        except:
                            raise
                except:
                    raise



    def update(self,request,viewset):
        from commercialoperator.components.proposals.utils import save_proponent_data
        with transaction.atomic():
            #import ipdb; ipdb.set_trace()
            if self.can_user_edit:
                # Save the data first
                save_proponent_data(self,request,viewset)
                self.save()
            else:
                raise ValidationError('You can\'t edit this proposal at this moment')


    def send_referral(self,request,referral_email,referral_text):
        with transaction.atomic():
            try:
                if self.processing_status == 'with_assessor' or self.processing_status == 'with_referral':
                    self.processing_status = 'with_referral'
                    self.save()
                    referral = None

                    # Check if the user is in ledger
                    try:
                        #user = EmailUser.objects.get(email__icontains=referral_email)
                        referral_group = ReferralRecipientGroup.objects.get(name__icontains=referral_email)
                    #except EmailUser.DoesNotExist:
                    except ReferralRecipientGroup.DoesNotExist:
                        raise exceptions.ProposalReferralCannotBeSent()
#                        # Validate if it is a deparment user
#                        department_user = get_department_user(referral_email)
#                        if not department_user:
#                            raise ValidationError('The user you want to send the referral to is not a member of the department')
#                        # Check if the user is in ledger or create
#                        #import ipdb; ipdb.set_trace()
#                        email = department_user['email'].lower()
#                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
#                        if created:
#                            user.first_name = department_user['given_name']
#                            user.last_name = department_user['surname']
#                            user.save()
                    try:
                        #Referral.objects.get(referral=user,proposal=self)
                        Referral.objects.get(referral_group=referral_group,proposal=self)
                        raise ValidationError('A referral has already been sent to this group')
                    except Referral.DoesNotExist:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self,
                            #referral=user,
                            referral_group=referral_group,
                            sent_by=request.user,
                            text=referral_text
                        )
                    # Create a log entry for the proposal
                    #self.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    self.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}'.format(referral_group.name)),request)
                    # Create a log entry for the organisation
                    #self.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    self.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.id,'{}'.format(referral_group.name)),request)
                    # send email
                    recipients = referral_group.members_list
                    send_referral_email_notification(referral,recipients,request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise

    def assign_officer(self,request,officer):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if not self.can_assess(officer):
                    raise ValidationError('The selected person is not authorised to be assigned to this proposal')
                if self.processing_status == 'with_approver':
                    if officer != self.assigned_approver:
                        self.assigned_approver = officer
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_APPROVER.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
                        # Create a log entry for the organisation
                        self.applicant.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_APPROVER.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
                else:
                    if officer != self.assigned_officer:
                        self.assigned_officer = officer
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_ASSESSOR.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
                        # Create a log entry for the organisation
                        self.applicant.log_user_action(ProposalUserAction.ACTION_ASSIGN_TO_ASSESSOR.format(self.id,'{}({})'.format(officer.get_full_name(),officer.email)),request)
            except:
                raise

    def assing_approval_level_document(self, request):
        with transaction.atomic():
            try:
                approval_level_document = request.data['approval_level_document']
                if approval_level_document != 'null':
                    try:
                        document = self.documents.get(input_name=str(approval_level_document))
                    except ProposalDocument.DoesNotExist:
                        document = self.documents.get_or_create(input_name=str(approval_level_document), name=str(approval_level_document))[0]
                    document.name = str(approval_level_document)
                    # commenting out below tow lines - we want to retain all past attachments - reversion can use them
                    #if document._file and os.path.isfile(document._file.path):
                    #    os.remove(document._file.path)
                    document._file = approval_level_document
                    document.save()
                    d=ProposalDocument.objects.get(id=document.id)
                    self.approval_level_document = d
                    comment = 'Approval Level Document Added: {}'.format(document.name)
                else:
                    self.approval_level_document = None
                    comment = 'Approval Level Document Deleted: {}'.format(request.data['approval_level_document_name'])
                #self.save()
                self.save(version_comment=comment) # to allow revision to be added to reversion history
                self.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_DOCUMENT.format(self.id),request)
                # Create a log entry for the organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_APPROVAL_LEVEL_DOCUMENT.format(self.id),request)
                return self
            except:
                raise

    def unassign(self,request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status == 'with_approver':
                    if self.assigned_approver:
                        self.assigned_approver = None
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_UNASSIGN_APPROVER.format(self.id),request)
                        # Create a log entry for the organisation
                        self.applicant.log_user_action(ProposalUserAction.ACTION_UNASSIGN_APPROVER.format(self.id),request)
                else:
                    if self.assigned_officer:
                        self.assigned_officer = None
                        self.save()
                        # Create a log entry for the proposal
                        self.log_user_action(ProposalUserAction.ACTION_UNASSIGN_ASSESSOR.format(self.id),request)
                        # Create a log entry for the organisation
                        self.applicant.log_user_action(ProposalUserAction.ACTION_UNASSIGN_ASSESSOR.format(self.id),request)
            except:
                raise

    def move_to_status(self,request,status, approver_comment):
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        if status in ['with_assessor','with_assessor_requirements','with_approver']:
            if self.processing_status == 'with_referral' or self.can_user_edit:
                raise ValidationError('You cannot change the current status at this time')
            if self.processing_status != status:
                if self.processing_status =='with_approver':
                    if approver_comment:
                        self.approver_comment = approver_comment
                        self.save()
                        send_proposal_approver_sendback_email_notification(request, self)
                self.processing_status = status
                self.save()

                # Create a log entry for the proposal
                if self.processing_status == self.PROCESSING_STATUS_WITH_ASSESSOR:
                    self.log_user_action(ProposalUserAction.ACTION_BACK_TO_PROCESSING.format(self.id),request)
                elif self.processing_status == self.PROCESSING_STATUS_WITH_ASSESSOR_REQUIREMENTS:
                    self.log_user_action(ProposalUserAction.ACTION_ENTER_REQUIREMENTS.format(self.id),request)
        else:
            raise ValidationError('The provided status cannot be found.')


    def reissue_approval(self,request,status):
        if not self.processing_status=='approved' :
            raise ValidationError('You cannot change the current status at this time')
        elif self.approval and self.approval.can_reissue:
            if self.__approver_group() in request.user.proposalapprovergroup_set.all():
                self.processing_status = status
                self.save()
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_REISSUE_APPROVAL.format(self.id),request)
            else:
                raise ValidationError('Cannot reissue Approval')
        else:
            raise ValidationError('Cannot reissue Approval')


    def proposed_decline(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_assessor':
                    raise ValidationError('You cannot propose to decline if it is not with assessor')

                reason = details.get('reason')
                ProposalDeclinedDetails.objects.update_or_create(
                    proposal = self,
                    defaults={'officer': request.user, 'reason': reason, 'cc_email': details.get('cc_email',None)}
                )
                self.proposed_decline_status = True
                approver_comment = ''
                self.move_to_status(request,'with_approver', approver_comment)
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_PROPOSED_DECLINE.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_DECLINE.format(self.id),request)

                send_approver_decline_email_notification(reason, request, self)
            except:
                raise

    def final_decline(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_approver':
                    raise ValidationError('You cannot decline if it is not with approver')

                proposal_decline, success = ProposalDeclinedDetails.objects.update_or_create(
                    proposal = self,
                    defaults={'officer':request.user,'reason':details.get('reason'),'cc_email':details.get('cc_email',None)}
                )
                self.proposed_decline_status = True
                self.processing_status = 'declined'
                self.customer_status = 'declined'
                self.save()
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.id),request)
                send_proposal_decline_email_notification(self,request, proposal_decline)
            except:
                raise

    def on_hold(self,request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if not (self.processing_status == 'with_assessor' or self.processing_status == 'with_referral'):
                    raise ValidationError('You cannot put on hold if it is not with assessor or with referral')

                self.prev_processing_status = self.processing_status
                self.processing_status = self.PROCESSING_STATUS_ONHOLD
                self.save()
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_PUT_ONHOLD.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_PUT_ONHOLD.format(self.id),request)

                #send_approver_decline_email_notification(reason, request, self)
            except:
                raise

    def on_hold_remove(self,request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'on_hold':
                    raise ValidationError('You cannot remove on hold if it is not currently on hold')

                self.processing_status = self.prev_processing_status
                self.prev_processing_status = self.PROCESSING_STATUS_ONHOLD
                self.save()
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_REMOVE_ONHOLD.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_REMOVE_ONHOLD.format(self.id),request)

                #send_approver_decline_email_notification(reason, request, self)
            except:
                raise

    def with_qaofficer(self,request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if not (self.processing_status == 'with_assessor' or self.processing_status == 'with_referral'):
                    raise ValidationError('You cannot send to QA Officer if it is not with assessor or with referral')

                self.prev_processing_status = self.processing_status
                self.processing_status = self.PROCESSING_STATUS_WITH_QA_OFFICER
                self.qaofficer_referral = True
                if self.qaofficer_referrals.exists():
                    qaofficer_referral = self.qaofficer_referrals.first()
                    qaofficer_referral.sent_by = request.user
                    qaofficer_referral.processing_status = 'with_qaofficer'
                else:
                    qaofficer_referral = self.qaofficer_referrals.create(sent_by=request.user)

                qaofficer_referral.save()
                self.save()

                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_WITH_QA_OFFICER.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_WITH_QA_OFFICER.format(self.id),request)

                #send_approver_decline_email_notification(reason, request, self)
                recipients = self.qa_officers()
                send_qaofficer_email_notification(self, recipients, request)

            except:
                raise

    def with_qaofficer_completed(self,request):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_qa_officer':
                    raise ValidationError('You cannot Complete QA Officer Assessment if processing status not currently With Assessor')

                self.processing_status = self.prev_processing_status
                self.prev_processing_status = self.PROCESSING_STATUS_WITH_QA_OFFICER

                qaofficer_referral = self.qaofficer_referrals.first()
                qaofficer_referral.qaofficer = request.user
                qaofficer_referral.qaofficer_group = QAOfficerGroup.objects.get(default=True)
                qaofficer_referral.qaofficer_text = request.data['text']
                qaofficer_referral.processing_status = 'completed'

                qaofficer_referral.save()
                self.save()

                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_QA_OFFICER_COMPLETED.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_QA_OFFICER_COMPLETED.format(self.id),request)

                #send_approver_decline_email_notification(reason, request, self)
                recipients = self.qa_officers()
                send_qaofficer_complete_email_notification(self, recipients, request)
            except:
                raise


    def proposed_approval(self,request,details):
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_assessor_requirements':
                    raise ValidationError('You cannot propose for approval if it is not with assessor for requirements')
                self.proposed_issuance_approval = {
                    'start_date' : details.get('start_date').strftime('%d/%m/%Y'),
                    'expiry_date' : details.get('expiry_date').strftime('%d/%m/%Y'),
                    'details': details.get('details'),
                    'cc_email':details.get('cc_email')
                }
                self.proposed_decline_status = False
                approver_comment = ''
                self.move_to_status(request,'with_approver', approver_comment)
                self.assigned_officer = None
                self.save()
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_PROPOSED_APPROVAL.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_PROPOSED_APPROVAL.format(self.id),request)

                send_approver_approve_email_notification(request, self)
            except:
                raise

    def final_approval(self,request,details):
        from commercialoperator.components.approvals.models import Approval
        with transaction.atomic():
            try:
                if not self.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.processing_status != 'with_approver':
                    raise ValidationError('You cannot issue the approval if it is not with an approver')
                if not self.applicant.organisation.postal_address:
                    raise ValidationError('The applicant needs to have set their postal address before approving this proposal.')

                self.proposed_issuance_approval = {
                    'start_date' : details.get('start_date').strftime('%d/%m/%Y'),
                    'expiry_date' : details.get('expiry_date').strftime('%d/%m/%Y'),
                    'details': details.get('details'),
                    'cc_email':details.get('cc_email')
                }
                self.proposed_decline_status = False
                self.processing_status = 'approved'
                self.customer_status = 'approved'
                # Log proposal action
                self.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.id),request)
                # Log entry for organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.id),request)

                if self.processing_status == 'approved':
                    # TODO if it is an ammendment proposal then check appropriately
                    #import ipdb; ipdb.set_trace()
                    checking_proposal = self
                    if self.proposal_type == 'renewal':
                        if self.previous_application:
                            previous_approval = self.previous_application.approval
                            approval,created = Approval.objects.update_or_create(
                                current_proposal = checking_proposal,
                                defaults = {
                                    #'activity' : self.activity,
                                    #'region' : self.region,
                                    #'tenure' : self.tenure,
                                    #'title' : self.title,
                                    'issue_date' : timezone.now(),
                                    'expiry_date' : details.get('expiry_date'),
                                    'start_date' : details.get('start_date'),
                                    'applicant' : self.applicant,
                                    'lodgement_number': previous_approval.lodgement_number
                                    #'extracted_fields' = JSONField(blank=True, null=True)
                                }
                            )
                            if created:
                                previous_approval.replaced_by = approval
                                previous_approval.save()

                    elif self.proposal_type == 'amendment':
                        if self.previous_application:
                            previous_approval = self.previous_application.approval
                            approval,created = Approval.objects.update_or_create(
                                current_proposal = checking_proposal,
                                defaults = {
                                    #'activity' : self.activity,
                                    #'region' : self.region,
                                    #'tenure' : self.tenure,
                                    #'title' : self.title,
                                    'issue_date' : timezone.now(),
                                    'expiry_date' : details.get('expiry_date'),
                                    'start_date' : details.get('start_date'),
                                    'applicant' : self.applicant,
                                    'lodgement_number': previous_approval.lodgement_number
                                    #'extracted_fields' = JSONField(blank=True, null=True)
                                }
                            )
                            if created:
                                previous_approval.replaced_by = approval
                                previous_approval.save()
                    else:
                        approval,created = Approval.objects.update_or_create(
                            current_proposal = checking_proposal,
                            defaults = {
                                #'activity' : self.activity,
                                #'region' : self.region.name,
                                #'tenure' : self.tenure.name,
                                #'title' : self.title,
                                'issue_date' : timezone.now(),
                                'expiry_date' : details.get('expiry_date'),
                                'start_date' : details.get('start_date'),
                                'applicant' : self.applicant
                                #'extracted_fields' = JSONField(blank=True, null=True)
                            }
                        )
                    # Generate compliances
                    #self.generate_compliances(approval, request)
                    from commercialoperator.components.compliances.models import Compliance, ComplianceUserAction
                    if created:
                        if self.proposal_type == 'amendment':
                            approval_compliances = Compliance.objects.filter(approval= previous_approval, proposal = self.previous_application, processing_status='future')
                            if approval_compliances:
                                for c in approval_compliances:
                                    c.delete()
                        # Log creation
                        # Generate the document
                        approval.generate_doc(request.user)
                        self.generate_compliances(approval, request)
                        # send the doc and log in approval and org
                    else:
                        #approval.replaced_by = request.user
                        approval.replaced_by = self.approval
                        # Generate the document
                        approval.generate_doc(request.user)
                        #Delete the future compliances if Approval is reissued and generate the compliances again.
                        approval_compliances = Compliance.objects.filter(approval= approval, proposal = self, processing_status='future')
                        if approval_compliances:
                            for c in approval_compliances:
                                c.delete()
                        self.generate_compliances(approval, request)
                        # Log proposal action
                        self.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.id),request)
                        # Log entry for organisation
                        self.applicant.log_user_action(ProposalUserAction.ACTION_UPDATE_APPROVAL_.format(self.id),request)
                    self.approval = approval
                #send Proposal approval email with attachment
                send_proposal_approval_email_notification(self,request)
                self.save(version_comment='Final Approval: {}'.format(self.approval.lodgement_number))
                self.approval.documents.all().update(can_delete=False)

            except:
                raise



    '''def generate_compliances(self,approval):
        from commercialoperator.components.compliances.models import Compliance
        today = timezone.now().date()
        timedelta = datetime.timedelta

        for req in self.requirements.all():
            if req.recurrence and req.due_date > today:
                current_date = req.due_date
                while current_date < approval.expiry_date:
                    for x in range(req.recurrence_schedule):
                    #Weekly
                        if req.recurrence_pattern == 1:
                            current_date += timedelta(weeks=1)
                    #Monthly
                        elif req.recurrence_pattern == 2:
                            current_date += timedelta(weeks=4)
                            pass
                    #Yearly
                        elif req.recurrence_pattern == 3:
                            current_date += timedelta(days=365)
                    # Create the compliance
                    if current_date <= approval.expiry_date:
                        Compliance.objects.create(
                            proposal=self,
                            due_date=current_date,
                            processing_status='future',
                            approval=approval,
                            requirement=req.requirement,
                        )
                        #TODO add logging for compliance'''


    def generate_compliances(self,approval, request):
        today = timezone.now().date()
        timedelta = datetime.timedelta
        from commercialoperator.components.compliances.models import Compliance, ComplianceUserAction
        #For amendment type of Proposal, check for copied requirements from previous proposal
        if self.proposal_type == 'amendment':
            try:
                for r in self.requirements.filter(copied_from__isnull=False):
                    cs=[]
                    cs=Compliance.objects.filter(requirement=r.copied_from, proposal=self.previous_application, processing_status='due')
                    if cs:
                        if r.is_deleted == True:
                            for c in cs:
                                c.processing_status='discarded'
                                c.customer_status = 'discarded'
                                c.reminder_sent=True
                                c.post_reminder_sent=True
                                c.save()
                        if r.is_deleted == False:
                            for c in cs:
                                c.proposal= self
                                c.approval=approval
                                c.requirement=r
                                c.save()
            except:
                raise
        #requirement_set= self.requirements.filter(copied_from__isnull=True).exclude(is_deleted=True)
        requirement_set= self.requirements.all().exclude(is_deleted=True)

        #for req in self.requirements.all():
        for req in requirement_set:
            try:
                if req.due_date and req.due_date >= today:
                    current_date = req.due_date
                    #create a first Compliance
                    try:
                        compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                    except Compliance.DoesNotExist:
                        compliance =Compliance.objects.create(
                                    proposal=self,
                                    due_date=current_date,
                                    processing_status='future',
                                    approval=approval,
                                    requirement=req,
                        )
                        compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.id),request)
                    if req.recurrence:
                        while current_date < approval.expiry_date:
                            for x in range(req.recurrence_schedule):
                            #Weekly
                                if req.recurrence_pattern == 1:
                                    current_date += timedelta(weeks=1)
                            #Monthly
                                elif req.recurrence_pattern == 2:
                                    current_date += timedelta(weeks=4)
                                    pass
                            #Yearly
                                elif req.recurrence_pattern == 3:
                                    current_date += timedelta(days=365)
                            # Create the compliance
                            if current_date <= approval.expiry_date:
                                try:
                                    compliance= Compliance.objects.get(requirement = req, due_date = current_date)
                                except Compliance.DoesNotExist:
                                    compliance =Compliance.objects.create(
                                                proposal=self,
                                                due_date=current_date,
                                                processing_status='future',
                                                approval=approval,
                                                requirement=req,
                                    )
                                    compliance.log_user_action(ComplianceUserAction.ACTION_CREATE.format(compliance.id),request)
            except:
                raise



    def renew_approval(self,request):
        with transaction.atomic():
            previous_proposal = self
            try:
                proposal=Proposal.objects.get(previous_application = previous_proposal)
                if proposal.customer_status=='with_assessor':
                    raise ValidationError('A renewal for this licence has already been lodged and is awaiting review.')
            except Proposal.DoesNotExist:
                previous_proposal = Proposal.objects.get(id=self.id)
                proposal = clone_proposal_with_status_reset(previous_proposal)
                proposal.proposal_type = 'renewal'
                #proposal.schema = ProposalType.objects.first().schema
                ptype = ProposalType.objects.filter(name=proposal.application_type).latest('version')
                proposal.schema = ptype.schema
                proposal.submitter = request.user
                proposal.previous_application = self
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_RENEW_PROPOSAL.format(self.id),request)
                # Create a log entry for the organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_RENEW_PROPOSAL.format(self.id),request)
                #Log entry for approval
                from commercialoperator.components.approvals.models import ApprovalUserAction
                self.approval.log_user_action(ApprovalUserAction.ACTION_RENEW_APPROVAL.format(self.approval.id),request)
                proposal.save(version_comment='New Amendment/Renewal Proposal created, from origin {}'.format(proposal.previous_application_id))
                #proposal.save()
            return proposal

    def amend_approval(self,request):
        with transaction.atomic():
            previous_proposal = self
            try:
                amend_conditions = {
                'previous_application': previous_proposal,
                'proposal_type': 'amendment'

                }
                proposal=Proposal.objects.get(**amend_conditions)
                if proposal.customer_status=='under_review':
                    raise ValidationError('An amendment for this licence has already been lodged and is awaiting review.')
            except Proposal.DoesNotExist:
                previous_proposal = Proposal.objects.get(id=self.id)
                proposal = clone_proposal_with_status_reset(previous_proposal)
                proposal.proposal_type = 'amendment'
                #proposal.schema = ProposalType.objects.first().schema
                ptype = ProposalType.objects.filter(name=proposal.application_type).latest('version')
                proposal.schema = ptype.schema
                proposal.submitter = request.user
                proposal.previous_application = self
                #copy all the requirements from the previous proposal
                #req=self.requirements.all()
                req=self.requirements.all().exclude(is_deleted=True)
                from copy import deepcopy
                if req:
                    for r in req:
                        old_r = deepcopy(r)
                        r.proposal = proposal
                        r.copied_from=old_r
                        r.id = None
                        r.save()
                # Create a log entry for the proposal
                self.log_user_action(ProposalUserAction.ACTION_AMEND_PROPOSAL.format(self.id),request)
                # Create a log entry for the organisation
                self.applicant.log_user_action(ProposalUserAction.ACTION_AMEND_PROPOSAL.format(self.id),request)
                #Log entry for approval
                from commercialoperator.components.approvals.models import ApprovalUserAction
                self.approval.log_user_action(ApprovalUserAction.ACTION_AMEND_APPROVAL.format(self.approval.id),request)
                proposal.save(version_comment='New Amendment/Renewal Proposal created, from origin {}'.format(proposal.previous_application_id))
                #proposal.save()
            return proposal


class ProposalLogDocument(Document):
    log_entry = models.ForeignKey('ProposalLogEntry',related_name='documents')
    _file = models.FileField(upload_to=update_proposal_comms_log_filename)

    class Meta:
        app_label = 'commercialoperator'

class ProposalLogEntry(CommunicationsLogEntry):
    proposal = models.ForeignKey(Proposal, related_name='comms_logs')

    class Meta:
        app_label = 'commercialoperator'

    def save(self, **kwargs):
        # save the application reference if the reference not provided
        if not self.reference:
            self.reference = self.proposal.reference
        super(ProposalLogEntry, self).save(**kwargs)

class ProposalOtherDetails(models.Model):
    #activities_land = models.CharField(max_length=24, blank=True, default='')
    # ACCREDITATION_TYPE_CHOICES = (
    #     ('no', 'No'),
    #     ('atap', 'ATAP'),
    #     ('eco_certification', 'Eco Certification'),
    #     ('narta', 'NARTA'),
    # )
    # LICENSE_PERIOD_CHOICES=(
    #     ('2_months','2 months'),
    #     ('1_year','1 Year'),
    #     ('3_year', '3 Years'),
    #     ('5_year', '5 Years'),
    #     ('7_year', '7 Years'),
    #     ('10_year', '10 Years'),
    # )
    LICENCE_PERIOD_CHOICES=(
        ('2_months','2 months'),
        ('1_year','1 Year'),
        ('3_year', '3 Years'),
        ('5_year', '5 Years'),
        ('7_year', '7 Years'),
        ('10_year', '10 Years'),
    )
    #accreditation_type = models.CharField('Accreditation', max_length=40, choices=ACCREDITATION_TYPE_CHOICES,
    #                                   default=ACCREDITATION_TYPE_CHOICES[0][0])
    #accreditation_expiry= models.DateTimeField(blank=True, null=True)
    #accreditation_expiry= models.DateField(blank=True, null=True)

    #preferred_license_period=models.CharField('Preferred license period', max_length=40, choices=LICENSE_PERIOD_CHOICES,default=LICENSE_PERIOD_CHOICES[0][0])
    preferred_licence_period=models.CharField('Preferred licence period', max_length=40, choices=LICENCE_PERIOD_CHOICES,default=LICENCE_PERIOD_CHOICES[0][0])
    #nominated_start_date= models.DateTimeField(blank=True, null=True)
    #insurance_expiry= models.DateTimeField(blank=True, null=True)
    nominated_start_date= models.DateField(blank=True, null=True)
    insurance_expiry= models.DateField(blank=True, null=True)
    other_comments=models.TextField(blank=True)
    #if credit facilities for payment of fees is required
    credit_fees=models.BooleanField(default=False)
    #if credit/ cash payment docket books are required
    credit_docket_books=models.BooleanField(default=False)
    proposal = models.OneToOneField(Proposal, related_name='other_details', null=True)

    class Meta:
        app_label = 'commercialoperator'

class ProposalAccreditation(models.Model):
    #activities_land = models.CharField(max_length=24, blank=True, default='')
    ACCREDITATION_TYPE_CHOICES = (
        ('no', 'No'),
        ('atap', 'ATAP'),
        ('eco_certification', 'Eco Certification'),
        ('narta', 'NARTA'),
        ('other', 'Other')
    )

    accreditation_type = models.CharField('Accreditation', max_length=40, choices=ACCREDITATION_TYPE_CHOICES,
                                       default=ACCREDITATION_TYPE_CHOICES[0][0])
    accreditation_expiry= models.DateField(blank=True, null=True)
    comments=models.TextField(blank=True)
    proposal_other_details = models.ForeignKey(ProposalOtherDetails, related_name='accreditations', null=True)

    class Meta:
        app_label = 'commercialoperator'



class ProposalPark(models.Model):
    park = models.ForeignKey(Park, blank=True, null=True, related_name='proposals')
    proposal = models.ForeignKey(Proposal, blank=True, null=True, related_name='parks')

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('park', 'proposal')

    @property
    def land_activities(self):
        qs=self.activities.all()
        categories=ActivityCategory.objects.filter(activity_type='land')
        activities=qs.filter(Q(activity__activity_category__in = categories)& Q(activity__visible=True))
        return activities

    @property
    def marine_activities(self):
        qs=self.activities.all()
        categories=ActivityCategory.objects.filter(activity_type='marine')
        activities=qs.filter(Q(activity__activity_category__in = categories)& Q(activity__visible=True))
        return activities


#To store Park activities related to Proposal T class land parks
class ProposalParkActivity(models.Model):
    proposal_park = models.ForeignKey(ProposalPark, blank=True, null=True, related_name='activities')
    activity = models.ForeignKey(Activity, blank=True, null=True)

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('proposal_park', 'activity')

#To store Park access_types related to Proposal T class land parks
class ProposalParkAccess(models.Model):
    proposal_park = models.ForeignKey(ProposalPark, blank=True, null=True, related_name='access_types')
    access_type = models.ForeignKey(AccessType, blank=True, null=True)

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('proposal_park', 'access_type')

#To store Park zones related to Proposal T class marine parks
class ProposalParkZone(models.Model):
    proposal_park = models.ForeignKey(ProposalPark, blank=True, null=True, related_name='zones')
    zone = models.ForeignKey(Zone, blank=True, null=True, related_name='proposal_zones')
    access_point = models.CharField(max_length=200, blank=True)


    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('zone', 'proposal_park')

class ProposalParkZoneActivity(models.Model):
    park_zone = models.ForeignKey(ProposalParkZone, blank=True, null=True, related_name='park_activities')
    activity = models.ForeignKey(Activity, blank=True, null=True)
    #section=models.ForeignKey(Section, blank=True, null= True)

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('park_zone', 'activity')


class ProposalTrail(models.Model):
    trail = models.ForeignKey(Trail, blank=True, null=True, related_name='proposals')
    proposal = models.ForeignKey(Proposal, blank=True, null=True, related_name='trails')

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('trail', 'proposal')

    # @property
    # def sections(self):
    #     qs=self.activities.all()
    #     categories=ActivityCategory.objects.filter(activity_type='land')
    #     activities=qs.filter(Q(activity__activity_category__in = categories)& Q(activity__visible=True))
    #     return activities

class ProposalTrailSection(models.Model):
    proposal_trail = models.ForeignKey(ProposalTrail, blank=True, null=True, related_name='sections')
    section = models.ForeignKey(Section, blank=True, null=True, related_name='proposal_trails')

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('section', 'proposal_trail')

#TODO: Need to remove this model
# class ProposalTrailActivity(models.Model):
#     proposal_trail = models.ForeignKey(ProposalTrail, blank=True, null=True, related_name='trail_activities')
#     activity = models.ForeignKey(Activity, blank=True, null=True)
#     section=models.ForeignKey(Section, blank=True, null= True)

#     class Meta:
#         app_label = 'commercialoperator'
#         unique_together = ('proposal_trail', 'activity')

class ProposalTrailSectionActivity(models.Model):
    trail_section = models.ForeignKey(ProposalTrailSection, blank=True, null=True, related_name='trail_activities')
    activity = models.ForeignKey(Activity, blank=True, null=True)
    #section=models.ForeignKey(Section, blank=True, null= True)

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('trail_section', 'activity')


@python_2_unicode_compatible
class Vehicle(models.Model):
    capacity = models.CharField(max_length=200, blank=True)
    rego = models.CharField(max_length=200, blank=True)
    license = models.CharField(max_length=200, blank=True)
    access_type= models.ForeignKey(AccessType,null=True, related_name='vehicles')
    rego_expiry= models.DateField(blank=True, null=True)
    proposal = models.ForeignKey(Proposal, related_name='vehicles')

    class Meta:
        app_label = 'commercialoperator'

    def __str__(self):
        return self.rego

@python_2_unicode_compatible
class Vessel(models.Model):
    nominated_vessel = models.CharField(max_length=200, blank=True)
    spv_no = models.CharField(max_length=200, blank=True)
    hire_rego = models.CharField(max_length=200, blank=True)
    craft_no = models.CharField(max_length=200, blank=True)
    size = models.CharField(max_length=200, blank=True)
    #rego_expiry= models.DateField(blank=True, null=True)
    proposal = models.ForeignKey(Proposal, related_name='vessels')

    class Meta:
        app_label = 'commercialoperator'

    def __str__(self):
        return self.nominated_vessel

class ProposalRequest(models.Model):
    proposal = models.ForeignKey(Proposal)
    subject = models.CharField(max_length=200, blank=True)
    text = models.TextField(blank=True)
    officer = models.ForeignKey(EmailUser, null=True)

    class Meta:
        app_label = 'commercialoperator'

class ComplianceRequest(ProposalRequest):
    REASON_CHOICES = (('outstanding', 'There are currently outstanding returns for the previous licence'),
                      ('other', 'Other'))
    reason = models.CharField('Reason', max_length=30, choices=REASON_CHOICES, default=REASON_CHOICES[0][0])

    class Meta:
        app_label = 'commercialoperator'


class AmendmentReason(models.Model):
    reason = models.CharField('Reason', max_length=125)

    class Meta:
        app_label = 'commercialoperator'
        verbose_name = "Proposal Amendment Reason" # display name in Admin
        verbose_name_plural = "Proposal Amendment Reasons"

    def __str__(self):
        return self.reason


class AmendmentRequest(ProposalRequest):
    STATUS_CHOICES = (('requested', 'Requested'), ('amended', 'Amended'))
    #REASON_CHOICES = (('insufficient_detail', 'The information provided was insufficient'),
    #                  ('missing_information', 'There was missing information'),
    #                  ('other', 'Other'))
    # try:
    #     # model requires some choices if AmendmentReason does not yet exist or is empty
    #     REASON_CHOICES = list(AmendmentReason.objects.values_list('id', 'reason'))
    #     if not REASON_CHOICES:
    #         REASON_CHOICES = ((0, 'The information provided was insufficient'),
    #                           (1, 'There was missing information'),
    #                           (2, 'Other'))
    # except:
    #     REASON_CHOICES = ((0, 'The information provided was insufficient'),
    #                       (1, 'There was missing information'),
    #                       (2, 'Other'))


    status = models.CharField('Status', max_length=30, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0])
    #reason = models.CharField('Reason', max_length=30, choices=REASON_CHOICES, default=REASON_CHOICES[0][0])
    reason = models.ForeignKey(AmendmentReason, blank=True, null=True)
    #reason = models.ForeignKey(AmendmentReason)

    class Meta:
        app_label = 'commercialoperator'

    def generate_amendment(self,request):
        with transaction.atomic():
            try:
                if not self.proposal.can_assess(request.user):
                    raise exceptions.ProposalNotAuthorized()
                if self.status == 'requested':
                    proposal = self.proposal
                    if proposal.processing_status != 'draft':
                        proposal.processing_status = 'draft'
                        proposal.customer_status = 'draft'
                        proposal.save()

                    # Create a log entry for the proposal
                    proposal.log_user_action(ProposalUserAction.ACTION_ID_REQUEST_AMENDMENTS,request)
                    # Create a log entry for the organisation
                    proposal.applicant.log_user_action(ProposalUserAction.ACTION_ID_REQUEST_AMENDMENTS,request)

                    # send email

                    send_amendment_email_notification(self,request, proposal)

                self.save()
            except:
                raise

class Assessment(ProposalRequest):
    STATUS_CHOICES = (('awaiting_assessment', 'Awaiting Assessment'), ('assessed', 'Assessed'),
                      ('assessment_expired', 'Assessment Period Expired'))
    assigned_assessor = models.ForeignKey(EmailUser, blank=True, null=True)
    status = models.CharField('Status', max_length=20, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0])
    date_last_reminded = models.DateField(null=True, blank=True)
    #requirements = models.ManyToManyField('Requirement', through='AssessmentRequirement')
    comment = models.TextField(blank=True)
    purpose = models.TextField(blank=True)

    class Meta:
        app_label = 'commercialoperator'

class ProposalDeclinedDetails(models.Model):
    proposal = models.OneToOneField(Proposal)
    officer = models.ForeignKey(EmailUser, null=False)
    reason = models.TextField(blank=True)
    cc_email = models.TextField(null=True)

    class Meta:
        app_label = 'commercialoperator'

class ProposalOnHold(models.Model):
    proposal = models.OneToOneField(Proposal)
    officer = models.ForeignKey(EmailUser, null=False)
    comment = models.TextField(blank=True)
    documents = models.ForeignKey(ProposalDocument, blank=True, null=True, related_name='onhold_documents')

    class Meta:
        app_label = 'commercialoperator'


@python_2_unicode_compatible
#class ProposalStandardRequirement(models.Model):
class ProposalStandardRequirement(RevisionedMixin):
    text = models.TextField()
    code = models.CharField(max_length=10, unique=True)
    obsolete = models.BooleanField(default=False)

    def __str__(self):
        return self.code

    class Meta:
        app_label = 'commercialoperator'

class ProposalRequirement(OrderedModel):
    RECURRENCE_PATTERNS = [(1, 'Weekly'), (2, 'Monthly'), (3, 'Yearly')]
    standard_requirement = models.ForeignKey(ProposalStandardRequirement,null=True,blank=True)
    free_requirement = models.TextField(null=True,blank=True)
    standard = models.BooleanField(default=True)
    proposal = models.ForeignKey(Proposal,related_name='requirements')
    due_date = models.DateField(null=True,blank=True)
    recurrence = models.BooleanField(default=False)
    recurrence_pattern = models.SmallIntegerField(choices=RECURRENCE_PATTERNS,default=1)
    recurrence_schedule = models.IntegerField(null=True,blank=True)
    copied_from = models.ForeignKey('self', on_delete=models.SET_NULL, blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    #order = models.IntegerField(default=1)

    class Meta:
        app_label = 'commercialoperator'


    @property
    def requirement(self):
        return self.standard_requirement.text if self.standard else self.free_requirement

class ProposalUserAction(UserAction):
    ACTION_CREATE_CUSTOMER_ = "Create customer {}"
    ACTION_CREATE_PROFILE_ = "Create profile {}"
    ACTION_LODGE_APPLICATION = "Lodge proposal {}"
    ACTION_ASSIGN_TO_ASSESSOR = "Assign proposal {} to {} as the assessor"
    ACTION_UNASSIGN_ASSESSOR = "Unassign assessor from proposal {}"
    ACTION_ASSIGN_TO_APPROVER = "Assign proposal {} to {} as the approver"
    ACTION_UNASSIGN_APPROVER = "Unassign approver from proposal {}"
    ACTION_ACCEPT_ID = "Accept ID"
    ACTION_RESET_ID = "Reset ID"
    ACTION_ID_REQUEST_UPDATE = 'Request ID update'
    ACTION_ACCEPT_CHARACTER = 'Accept character'
    ACTION_RESET_CHARACTER = "Reset character"
    ACTION_ACCEPT_REVIEW = 'Accept review'
    ACTION_RESET_REVIEW = "Reset review"
    ACTION_ID_REQUEST_AMENDMENTS = "Request amendments"
    ACTION_SEND_FOR_ASSESSMENT_TO_ = "Send for assessment to {}"
    ACTION_SEND_ASSESSMENT_REMINDER_TO_ = "Send assessment reminder to {}"
    ACTION_DECLINE = "Decline proposal {}"
    ACTION_ENTER_CONDITIONS = "Enter requirement"
    ACTION_CREATE_CONDITION_ = "Create requirement {}"
    ACTION_ISSUE_APPROVAL_ = "Issue Approval for proposal {}"
    ACTION_UPDATE_APPROVAL_ = "Update Approval for proposal {}"
    ACTION_EXPIRED_APPROVAL_ = "Expire Approval for proposal {}"
    ACTION_DISCARD_PROPOSAL = "Discard proposal {}"
    ACTION_APPROVAL_LEVEL_DOCUMENT = "Assign Approval level document {}"
    # Assessors
    ACTION_SAVE_ASSESSMENT_ = "Save assessment {}"
    ACTION_CONCLUDE_ASSESSMENT_ = "Conclude assessment {}"
    ACTION_PROPOSED_APPROVAL = "Proposal {} has been proposed for approval"
    ACTION_PROPOSED_DECLINE = "Proposal {} has been proposed for decline"
    # Referrals
    ACTION_SEND_REFERRAL_TO = "Send referral {} for proposal {} to {}"
    ACTION_RESEND_REFERRAL_TO = "Resend referral {} for proposal {} to {}"
    ACTION_REMIND_REFERRAL = "Send reminder for referral {} for proposal {} to {}"
    ACTION_ENTER_REQUIREMENTS = "Enter Requirements for proposal {}"
    ACTION_BACK_TO_PROCESSING = "Back to processing for proposal {}"
    RECALL_REFERRAL = "Referral {} for proposal {} has been recalled"
    CONCLUDE_REFERRAL = "{}: Referral {} for proposal {} has been concluded by group {}"
    ACTION_REFERRAL_DOCUMENT = "Assign Referral document {}"
    #Approval
    ACTION_REISSUE_APPROVAL = "Reissue approval for proposal {}"
    ACTION_CANCEL_APPROVAL = "Cancel approval for proposal {}"
    ACTION_SUSPEND_APPROVAL = "Suspend approval for proposal {}"
    ACTION_REINSTATE_APPROVAL = "Reinstate approval for proposal {}"
    ACTION_SURRENDER_APPROVAL = "Surrender approval for proposal {}"
    ACTION_RENEW_PROPOSAL = "Create Renewal proposal for proposal {}"
    ACTION_AMEND_PROPOSAL = "Create Amendment proposal for proposal {}"
    #Vehicle
    ACTION_CREATE_VEHICLE = "Create Vehicle {}"
    ACTION_EDIT_VEHICLE = "Edit Vehicle {}"
    #Vessel
    ACTION_CREATE_VESSEL = "Create Vessel {}"
    ACTION_EDIT_VESSEL= "Edit Vessel {}"
    ACTION_PUT_ONHOLD = "Put Proposal On-hold {}"
    ACTION_REMOVE_ONHOLD = "Remove Proposal On-hold {}"
    ACTION_WITH_QA_OFFICER = "Send Proposal QA Officer {}"
    ACTION_QA_OFFICER_COMPLETED = "QA Officer Assessment Completed {}"


    class Meta:
        app_label = 'commercialoperator'
        ordering = ('-when',)

    @classmethod
    def log_action(cls, proposal, action, user):
        return cls.objects.create(
            proposal=proposal,
            who=user,
            what=str(action)
        )

    proposal = models.ForeignKey(Proposal, related_name='action_logs')


class ReferralRecipientGroup(models.Model):
    #site = models.OneToOneField(Site, default='1')
    name = models.CharField(max_length=30, unique=True)
    members = models.ManyToManyField(EmailUser)

    def __str__(self):
        return 'Referral Recipient Group'

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    @property
    def members_list(self):
            return list(self.members.all().values_list('email', flat=True))

    class Meta:
        app_label = 'commercialoperator'
        verbose_name_plural = "Referral recipient group"

class QAOfficerGroup(models.Model):
    #site = models.OneToOneField(Site, default='1')
    name = models.CharField(max_length=30, unique=True)
    members = models.ManyToManyField(EmailUser)
    default = models.BooleanField(default=False)

    def __str__(self):
        return 'QA Officer Group'

    @property
    def all_members(self):
        all_members = []
        all_members.extend(self.members.all())
        member_ids = [m.id for m in self.members.all()]
        #all_members.extend(EmailUser.objects.filter(is_superuser=True,is_staff=True,is_active=True).exclude(id__in=member_ids))
        return all_members

    @property
    def filtered_members(self):
        return self.members.all()

    @property
    def members_list(self):
            return list(self.members.all().values_list('email', flat=True))

    class Meta:
        app_label = 'commercialoperator'
        verbose_name_plural = "QA Officer group"


    def _clean(self):
        try:
            default = QAOfficerGroup.objects.get(default=True)
        except ProposalAssessorGroup.DoesNotExist:
            default = None

        if default and self.default:
            raise ValidationError('There can only be one default proposal QA Officer group')

    @property
    def current_proposals(self):
        assessable_states = ['with_qa_officer']
        return Proposal.objects.filter(processing_status__in=assessable_states)


#
#class ReferralRequestUserAction(UserAction):
#    ACTION_LODGE_REQUEST = "Lodge request {}"
#    ACTION_ASSIGN_TO = "Assign to {}"
#    ACTION_UNASSIGN = "Unassign"
#    ACTION_DECLINE_REQUEST = "Decline request"
#    # Assessors
#
#    ACTION_CONCLUDE_REQUEST = "Conclude request {}"
#
#    @classmethod
#    def log_action(cls, request, action, user):
#        return cls.objects.create(
#            request=request,
#            who=user,
#            what=str(action)
#        )
#
#    request = models.ForeignKey(ReferralRequest,related_name='action_logs')
#
#    class Meta:
#        app_label = 'commercialoperator'


#class Referral(models.Model):
class Referral(RevisionedMixin):
    SENT_CHOICES = (
        (1,'Sent From Assessor'),
        (2,'Sent From Referral')
    )
    PROCESSING_STATUS_CHOICES = (
                                 ('with_referral', 'Awaiting'),
                                 ('recalled', 'Recalled'),
                                 ('completed', 'Completed'),
                                 )
    lodged_on = models.DateTimeField(auto_now_add=True)
    proposal = models.ForeignKey(Proposal,related_name='referrals')
    sent_by = models.ForeignKey(EmailUser,related_name='commercialoperator_assessor_referrals')
    referral = models.ForeignKey(EmailUser,null=True,blank=True,related_name='commercialoperator_referalls')
    referral_group = models.ForeignKey(ReferralRecipientGroup,null=True,blank=True,related_name='commercialoperator_referral_groups')
    linked = models.BooleanField(default=False)
    sent_from = models.SmallIntegerField(choices=SENT_CHOICES,default=SENT_CHOICES[0][0])
    processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
                                         default=PROCESSING_STATUS_CHOICES[0][0])
    text = models.TextField(blank=True) #Assessor text
    referral_text = models.TextField(blank=True)
    document = models.ForeignKey(ReferralDocument, blank=True, null=True, related_name='referral_document')


    class Meta:
        app_label = 'commercialoperator'
        ordering = ('-lodged_on',)

    def __str__(self):
        return 'Proposal {} - Referral {}'.format(self.proposal.id,self.id)

    # Methods
    @property
    def latest_referrals(self):
        return Referral.objects.filter(sent_by=self.referral, proposal=self.proposal)[:2]

    @property
    def can_be_completed(self):
        return True
        #Referral cannot be completed until second level referral sent by referral has been completed/recalled
        qs=Referral.objects.filter(sent_by=self.referral, proposal=self.proposal, processing_status='with_referral')
        if qs:
            return False
        else:
            return True

    def recall(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.processing_status = 'recalled'
            self.save()
            # TODO Log proposal action
            self.proposal.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id,self.proposal.id),request)
            # TODO log organisation action
            self.proposal.applicant.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id,self.proposal.id),request)

    def remind(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            # Create a log entry for the proposal
            #self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
            # Create a log entry for the organisation
            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
            # send email
            recipients = self.referral_group.members_list
            send_referral_email_notification(self,recipients,request,reminder=True)

    def resend(self,request):
        with transaction.atomic():
            if not self.proposal.can_assess(request.user):
                raise exceptions.ProposalNotAuthorized()
            self.processing_status = 'with_referral'
            self.proposal.processing_status = 'with_referral'
            self.proposal.save()
            self.sent_from = 1
            self.save()
            # Create a log entry for the proposal
            #self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
            # Create a log entry for the organisation
            #self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
            # send email
            recipients = self.referral_group.members_list
            send_referral_email_notification(self,recipients,request)

    def complete(self,request):
        with transaction.atomic():
            try:
                #if request.user != self.referral:
                group =  ReferralRecipientGroup.objects.filter(name=self.referral_group)
                if group and group[0] in u.referralrecipientgroup_set.all():
                    raise exceptions.ReferralNotAuthorized()
                self.processing_status = 'completed'
                self.referral = request.user
                self.referral_text = request.user.get_full_name() + ': ' + request.data.get('referral_comment')
                self.add_referral_document(request)
                self.save()
                # TODO Log proposal action
                #self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(request.user.get_full_name(), self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
                # TODO log organisation action
                #self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
                self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(request.user.get_full_name(), self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
                send_referral_complete_email_notification(self,request)
            except:
                raise

    def add_referral_document(self, request):
        with transaction.atomic():
            try:
                referral_document = request.data['referral_document']
                #import ipdb; ipdb.set_trace()
                if referral_document != 'null':
                    try:
                        document = self.referral_documents.get(input_name=str(referral_document))
                    except ReferralDocument.DoesNotExist:
                        document = self.referral_documents.get_or_create(input_name=str(referral_document), name=str(referral_document))[0]
                    document.name = str(referral_document)
                    # commenting out below tow lines - we want to retain all past attachments - reversion can use them
                    #if document._file and os.path.isfile(document._file.path):
                    #    os.remove(document._file.path)
                    document._file = referral_document
                    document.save()
                    d=ReferralDocument.objects.get(id=document.id)
                    self.referral_document = d
                    comment = 'Referral Document Added: {}'.format(document.name)
                else:
                    self.referral_document = None
                    comment = 'Referral Document Deleted: {}'.format(request.data['referral_document_name'])
                #self.save()
                self.save(version_comment=comment) # to allow revision to be added to reversion history
                self.proposal.log_user_action(ProposalUserAction.ACTION_REFERRAL_DOCUMENT.format(self.id),request)
                # Create a log entry for the organisation
                self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_REFERRAL_DOCUMENT.format(self.id),request)
                return self
            except:
                raise


    def send_referral(self,request,referral_email,referral_text):
        with transaction.atomic():
            try:
                #import ipdb; ipdb.set_trace()
                if self.proposal.processing_status == 'with_referral':
                    if request.user != self.referral:
                        raise exceptions.ReferralNotAuthorized()
                    if self.sent_from != 1:
                        raise exceptions.ReferralCanNotSend()
                    self.proposal.processing_status = 'with_referral'
                    self.proposal.save()
                    referral = None
                    # Check if the user is in ledger
                    try:
                        user = EmailUser.objects.get(email__icontains=referral_email)
                    except EmailUser.DoesNotExist:
                        # Validate if it is a deparment user
                        department_user = get_department_user(referral_email)
                        if not department_user:
                            raise ValidationError('The user you want to send the referral to is not a member of the department')
                        # Check if the user is in ledger or create

                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
                        if created:
                            user.first_name = department_user['given_name']
                            user.last_name = department_user['surname']
                            user.save()
                    qs=Referral.objects.filter(sent_by=user, proposal=self.proposal)
                    if qs:
                        raise ValidationError('You cannot send referral to this user')
                    try:
                        Referral.objects.get(referral=user,proposal=self.proposal)
                        raise ValidationError('A referral has already been sent to this user')
                    except Referral.DoesNotExist:
                        # Create Referral
                        referral = Referral.objects.create(
                            proposal = self.proposal,
                            referral=user,
                            sent_by=request.user,
                            sent_from=2,
                            text=referral_text
                        )
                    # Create a log entry for the proposal
                    self.proposal.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # Create a log entry for the organisation
                    self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.id,'{}({})'.format(user.get_full_name(),user.email)),request)
                    # send email
                    recipients = self.email_group.members_list
                    send_referral_email_notification(referral,recipients,request)
                else:
                    raise exceptions.ProposalReferralCannotBeSent()
            except:
                raise


    # Properties
    @property
    def region(self):
        return self.proposal.region

    @property
    def activity(self):
        return self.proposal.activity

    @property
    def title(self):
        return self.proposal.title

    @property
    def applicant(self):
        return self.proposal.applicant.name

    @property
    def can_be_processed(self):
        return self.processing_status == 'with_referral'

    def can_assess_referral(self,user):
        return self.processing_status == 'with_referral'

class QAOfficerReferral(RevisionedMixin):
    SENT_CHOICES = (
        (1,'Sent From Assessor'),
        (2,'Sent From Referral')
    )
    PROCESSING_STATUS_CHOICES = (
                                 ('with_qaofficer', 'Awaiting'),
                                 ('recalled', 'Recalled'),
                                 ('completed', 'Completed'),
                                 )
    lodged_on = models.DateTimeField(auto_now_add=True)
    proposal = models.ForeignKey(Proposal,related_name='qaofficer_referrals')
    sent_by = models.ForeignKey(EmailUser,related_name='assessor_qaofficer_referrals')
    qaofficer = models.ForeignKey(EmailUser, null=True, blank=True, related_name='qaofficers')
    qaofficer_group = models.ForeignKey(QAOfficerGroup,null=True,blank=True,related_name='qaofficer_groups')
    linked = models.BooleanField(default=False)
    sent_from = models.SmallIntegerField(choices=SENT_CHOICES,default=SENT_CHOICES[0][0])
    processing_status = models.CharField('Processing Status', max_length=30, choices=PROCESSING_STATUS_CHOICES,
                                         default=PROCESSING_STATUS_CHOICES[0][0])
    text = models.TextField(blank=True) #Assessor text
    qaofficer_text = models.TextField(blank=True)
    document = models.ForeignKey(QAOfficerDocument, blank=True, null=True, related_name='qaofficer_referral_document')


    class Meta:
        app_label = 'commercialoperator'
        ordering = ('-lodged_on',)

    def __str__(self):
        return 'Proposal {} - QA Officer referral {}'.format(self.proposal.id,self.id)

    # Methods
    @property
    def latest_qaofficer_referrals(self):
        return QAOfficer.objects.filter(sent_by=self.qaofficer, proposal=self.proposal)[:2]

#    @property
#    def can_be_completed(self):
#        #Referral cannot be completed until second level referral sent by referral has been completed/recalled
#        qs=Referral.objects.filter(sent_by=self.referral, proposal=self.proposal, processing_status='with_referral')
#        if qs:
#            return False
#        else:
#            return True
#
#    def recall(self,request):
#        with transaction.atomic():
#            if not self.proposal.can_assess(request.user):
#                raise exceptions.ProposalNotAuthorized()
#            self.processing_status = 'recalled'
#            self.save()
#            # TODO Log proposal action
#            self.proposal.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id,self.proposal.id),request)
#            # TODO log organisation action
#            self.proposal.applicant.log_user_action(ProposalUserAction.RECALL_REFERRAL.format(self.id,self.proposal.id),request)
#
#    def remind(self,request):
#        with transaction.atomic():
#            if not self.proposal.can_assess(request.user):
#                raise exceptions.ProposalNotAuthorized()
#            # Create a log entry for the proposal
#            #self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
#            self.proposal.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
#            # Create a log entry for the organisation
#            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_REMIND_REFERRAL.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
#            # send email
#            recipients = self.referral_group.members_list
#            send_referral_email_notification(self,recipients,request,reminder=True)
#
#    def resend(self,request):
#        with transaction.atomic():
#            if not self.proposal.can_assess(request.user):
#                raise exceptions.ProposalNotAuthorized()
#            self.processing_status = 'with_referral'
#            self.proposal.processing_status = 'with_referral'
#            self.proposal.save()
#            self.sent_from = 1
#            self.save()
#            # Create a log entry for the proposal
#            #self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
#            self.proposal.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
#            # Create a log entry for the organisation
#            #self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
#            self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
#            # send email
#            recipients = self.referral_group.members_list
#            send_referral_email_notification(self,recipients,request)
#
#    def complete(self,request):
#        with transaction.atomic():
#            try:
#                #if request.user != self.referral:
#                group =  ReferralRecipientGroup.objects.filter(name=self.referral_group)
#                if group and group[0] in u.referralrecipientgroup_set.all():
#                    raise exceptions.ReferralNotAuthorized()
#                self.processing_status = 'completed'
#                self.referral = request.user
#                self.referral_text = request.user.get_full_name() + ': ' + request.data.get('referral_comment')
#                self.add_referral_document(request)
#                self.save()
#                # TODO Log proposal action
#                #self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
#                self.proposal.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(request.user.get_full_name(), self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
#                # TODO log organisation action
#                #self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(self.id,self.proposal.id,'{}({})'.format(self.referral.get_full_name(),self.referral.email)),request)
#                self.proposal.applicant.log_user_action(ProposalUserAction.CONCLUDE_REFERRAL.format(request.user.get_full_name(), self.id,self.proposal.id,'{}'.format(self.referral_group.name)),request)
#                send_referral_complete_email_notification(self,request)
#            except:
#                raise
#
#    def add_referral_document(self, request):
#        with transaction.atomic():
#            try:
#                referral_document = request.data['referral_document']
#                #import ipdb; ipdb.set_trace()
#                if referral_document != 'null':
#                    try:
#                        document = self.referral_documents.get(input_name=str(referral_document))
#                    except ReferralDocument.DoesNotExist:
#                        document = self.referral_documents.get_or_create(input_name=str(referral_document), name=str(referral_document))[0]
#                    document.name = str(referral_document)
#                    # commenting out below tow lines - we want to retain all past attachments - reversion can use them
#                    #if document._file and os.path.isfile(document._file.path):
#                    #    os.remove(document._file.path)
#                    document._file = referral_document
#                    document.save()
#                    d=ReferralDocument.objects.get(id=document.id)
#                    self.referral_document = d
#                    comment = 'Referral Document Added: {}'.format(document.name)
#                else:
#                    self.referral_document = None
#                    comment = 'Referral Document Deleted: {}'.format(request.data['referral_document_name'])
#                #self.save()
#                self.save(version_comment=comment) # to allow revision to be added to reversion history
#                self.proposal.log_user_action(ProposalUserAction.ACTION_REFERRAL_DOCUMENT.format(self.id),request)
#                # Create a log entry for the organisation
#                self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_REFERRAL_DOCUMENT.format(self.id),request)
#                return self
#            except:
#                raise
#
#
#    def send_referral(self,request,referral_email,referral_text):
#        with transaction.atomic():
#            try:
#                #import ipdb; ipdb.set_trace()
#                if self.proposal.processing_status == 'with_referral':
#                    if request.user != self.referral:
#                        raise exceptions.ReferralNotAuthorized()
#                    if self.sent_from != 1:
#                        raise exceptions.ReferralCanNotSend()
#                    self.proposal.processing_status = 'with_referral'
#                    self.proposal.save()
#                    referral = None
#                    # Check if the user is in ledger
#                    try:
#                        user = EmailUser.objects.get(email__icontains=referral_email)
#                    except EmailUser.DoesNotExist:
#                        # Validate if it is a deparment user
#                        department_user = get_department_user(referral_email)
#                        if not department_user:
#                            raise ValidationError('The user you want to send the referral to is not a member of the department')
#                        # Check if the user is in ledger or create
#
#                        user,created = EmailUser.objects.get_or_create(email=department_user['email'].lower())
#                        if created:
#                            user.first_name = department_user['given_name']
#                            user.last_name = department_user['surname']
#                            user.save()
#                    qs=Referral.objects.filter(sent_by=user, proposal=self.proposal)
#                    if qs:
#                        raise ValidationError('You cannot send referral to this user')
#                    try:
#                        Referral.objects.get(referral=user,proposal=self.proposal)
#                        raise ValidationError('A referral has already been sent to this user')
#                    except Referral.DoesNotExist:
#                        # Create Referral
#                        referral = Referral.objects.create(
#                            proposal = self.proposal,
#                            referral=user,
#                            sent_by=request.user,
#                            sent_from=2,
#                            text=referral_text
#                        )
#                    # Create a log entry for the proposal
#                    self.proposal.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.id,'{}({})'.format(user.get_full_name(),user.email)),request)
#                    # Create a log entry for the organisation
#                    self.proposal.applicant.log_user_action(ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(referral.id,self.proposal.id,'{}({})'.format(user.get_full_name(),user.email)),request)
#                    # send email
#                    recipients = self.email_group.members_list
#                    send_referral_email_notification(referral,recipients,request)
#                else:
#                    raise exceptions.ProposalReferralCannotBeSent()
#            except:
#                raise


    # Properties
    @property
    def region(self):
        return self.proposal.region

    @property
    def activity(self):
        return self.proposal.activity

    @property
    def title(self):
        return self.proposal.title

    @property
    def applicant(self):
        return self.proposal.applicant.name

    @property
    def can_be_processed(self):
        return self.processing_status == 'with_qa_officer'

    def can_asses(self):
        return self.can_be_processed and self.proposal.is_qa_officer()


@receiver(pre_delete, sender=Proposal)
def delete_documents(sender, instance, *args, **kwargs):
    for document in instance.documents.all():
        document.delete()

def clone_proposal_with_status_reset(proposal):
        with transaction.atomic():
            try:
                proposal.customer_status = 'draft'
                proposal.processing_status = 'draft'
                proposal.assessor_data = None
                proposal.comment_data = None

                #proposal.id_check_status = 'not_checked'
                #proposal.character_check_status = 'not_checked'
                #proposal.compliance_check_status = 'not_checked'
                #Sproposal.review_status = 'not_reviewed'

                proposal.lodgement_number = ''
                proposal.lodgement_sequence = 0
                proposal.lodgement_date = None

                proposal.assigned_officer = None
                proposal.assigned_approver = None

                proposal.approval = None

                original_proposal_id = proposal.id

                #proposal.previous_application = Proposal.objects.get(id=original_proposal_id)

                proposal.id = None
                proposal.approval_level_document = None

                proposal.save(no_revision=True)

                # clone documents
                for proposal_document in ProposalDocument.objects.filter(proposal=original_proposal_id):
                    proposal_document.proposal = proposal
                    proposal_document.id = None
                    proposal_document._file.name = u'proposals/{}/documents/{}'.format(proposal.id, proposal_document.name)
                    proposal_document.can_delete = True
                    proposal_document.save()

                # copy documents on file system and reset can_delete flag
                subprocess.call('cp -pr media/proposals/{} media/proposals/{}'.format(original_proposal_id, proposal.id), shell=True)

                return proposal
            except:
                raise

def searchKeyWords(searchWords, searchProposal, searchApproval, searchCompliance, is_internal= True):
    from commercialoperator.utils import search, search_approval, search_compliance
    from commercialoperator.components.approvals.models import Approval
    from commercialoperator.components.compliances.models import Compliance
    qs = []
    if is_internal:
        proposal_list = Proposal.objects.filter(application_type__name='T Class').exclude(processing_status__in=['discarded','draft'])
        approval_list = Approval.objects.all().order_by('lodgement_number', '-issue_date').distinct('lodgement_number')
        compliance_list = Compliance.objects.all()
    if searchWords:
        if searchProposal:
            for p in proposal_list:
                if p.data:
                    try:
                        results = search(p.data[0], searchWords)
                        final_results = {}
                        if results:
                            for r in results:
                                for key, value in r.iteritems():
                                    final_results.update({'key': key, 'value': value})
                            res = {
                                'number': p.lodgement_number,
                                'id': p.id,
                                'type': 'Proposal',
                                'applicant': p.applicant.name,
                                'text': final_results,
                                }
                            qs.append(res)
                    except:
                        raise
        if searchApproval:
            for a in approval_list:
                try:
                    results = search_approval(a, searchWords)
                    qs.extend(results)
                except:
                    raise
        if searchCompliance:
            for c in compliance_list:
                try:
                    results = search_compliance(c, searchWords)
                    qs.extend(results)
                except:
                    raise
    return qs

def search_reference(reference_number):
    from commercialoperator.components.approvals.models import Approval
    from commercialoperator.components.compliances.models import Compliance
    proposal_list = Proposal.objects.all().exclude(processing_status__in=['discarded'])
    approval_list = Approval.objects.all().order_by('lodgement_number', '-issue_date').distinct('lodgement_number')
    compliance_list = Compliance.objects.all().exclude(processing_status__in=['future'])
    record = {}
    try:
        result = proposal_list.get(lodgement_number = reference_number)
        record = {  'id': result.id,
                    'type': 'proposal' }
    except Proposal.DoesNotExist:
        try:
            result = approval_list.get(lodgement_number = reference_number)
            record = {  'id': result.id,
                        'type': 'approval' }
        except Approval.DoesNotExist:
            try:
                for c in compliance_list:
                    if c.reference == reference_number:
                        record = {  'id': c.id,
                                    'type': 'compliance' }
            except:
                raise ValidationError('Record with provided reference number does not exist')
    if record:
        return record
    else:
        raise ValidationError('Record with provided reference number does not exist')








from ckeditor.fields import RichTextField
class HelpPage(models.Model):
    HELP_TEXT_EXTERNAL = 1
    HELP_TEXT_INTERNAL = 2
    HELP_TYPE_CHOICES = (
        (HELP_TEXT_EXTERNAL, 'External'),
        (HELP_TEXT_INTERNAL, 'Internal'),
    )

    application_type = models.ForeignKey(ApplicationType)
    content = RichTextField()
    description = models.CharField(max_length=256, blank=True, null=True)
    help_type = models.SmallIntegerField('Help Type', choices=HELP_TYPE_CHOICES, default=HELP_TEXT_EXTERNAL)
    version = models.SmallIntegerField(default=1, blank=False, null=False)

    class Meta:
        app_label = 'commercialoperator'
        unique_together = ('application_type', 'help_type', 'version')


import reversion
reversion.register(Proposal, follow=['requirements', 'documents', 'compliances', 'referrals', 'approvals',])
reversion.register(ProposalType)
reversion.register(ProposalRequirement)            # related_name=requirements
reversion.register(ProposalStandardRequirement)    # related_name=proposal_requirements
reversion.register(ProposalDocument)               # related_name=documents
reversion.register(ProposalLogEntry)
reversion.register(ProposalUserAction)
reversion.register(ComplianceRequest)
reversion.register(AmendmentRequest)
reversion.register(Assessment)
reversion.register(Referral)
reversion.register(HelpPage)
reversion.register(ApplicationType)
reversion.register(ReferralRecipientGroup)
reversion.register(QAOfficerGroup)


