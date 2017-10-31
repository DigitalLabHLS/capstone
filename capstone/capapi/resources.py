from datetime import datetime
import logging
import zipfile
import tempfile

from django.conf import settings
from django.core.mail import send_mail
from django.template.defaultfilters import slugify

from scripts.helpers import parse_xml
from capdb.models import CaseXML, Jurisdiction, CaseMetadata

logger = logging.getLogger(__name__)


def get_matching_case_xml(case_id):
    try:
        xml = CaseXML.objects.get(case_id=case_id)
        # TODO: parse XML here
        return xml.orig_xml
    except CaseXML.DoesNotExist:
        logger.error("Case id mismatch", case_id)


def create_zip(case_list):
    # tmp file backed by RAM up to 10MB, then stored to disk
    tmp_file = tempfile.SpooledTemporaryFile(10 * 2 ** 20)
    with zipfile.ZipFile(tmp_file, 'w', zipfile.ZIP_DEFLATED) as archive:
        for case in case_list:
            archive.writestr(case.slug + '.xml', get_matching_case_xml(case.case_id))

    # Reset file pointer
    tmp_file.seek(0)

    # return open file handle
    return tmp_file


def create_zip_filename(case_list):
    ts = slugify(datetime.now().timestamp())
    if len(case_list) == 1:
        return case_list[0].slug + '-' + ts + '.zip'

    return '{0}_{1}_{2}.zip'.format(case_list[0].slug[:20], case_list[-1].slug[:20], ts)


def email(reason, user):
    title = 'CAP API: %s' % reason
    if reason == 'new_registration':
        message = "user %s %s at %s has requested API access." % (
            user.first_name,
            user.last_name,
            user.email
        )
        send_mail(
            title,
            message,
            settings.API_ADMIN_EMAIL_ADDRESS,
            [settings.API_EMAIL_ADDRESS]
        )
        logger.info("sent new_registration email for %s" % user.email)

    if reason == 'new_signup':
        token_url = os.path.join(settings.API_BASE_URL, "accounts/verify-user", str(user.id), user.get_activation_nonce())
        send_mail(
            'CaseLaw Access Project: Verify your email address',
            f"""Please click here to verify your email address: {token_url}
            If you believe you have received this message in error, please ignore it.
            """,
            settings.API_EMAIL_ADDRESS,
            [user.email],
            fail_silently=False, )
        logger.info("sent new_signup email for %s" % user.email)


def extract_casebody(case_xml):
    # strip soft hyphens from line endings
    text = case_xml.replace(u'\xad', '')
    case = parse_xml(text)

    # strip labels from footnotes:
    for footnote in case('casebody|footnote'):
        label = footnote.attrib.get('label')
        if label and footnote[0].text.startswith(label):
            footnote[0].text = footnote[0].text[len(label):]

    return case('casebody|casebody').html()


def jsonify_case(case_id):
    case_xml = get_matching_case_xml(case_id)
    case_metadata = CaseMetadata.objects.get(case_id=case_id)
    casebody = extract_casebody(case_xml)

    return {
        'slug': case_metadata.slug,
        '':'',
        'casebody': casebody
    }

#
# """       <court abbreviation="Mass." jurisdiction="Massachusetts">Massachusetts Supreme Judicial Court</court>
#           <name abbreviation="Opinion of the Justices to the Senate &amp; House of Representatives">Opinion of the Justices to the Senate and House of Representatives</name>
#           <docketnumber/>
#           <citation category="official" type="bluebook">126 Mass. 557</citation>
#           <decisiondate>1781-02-22</decisiondate>
# """