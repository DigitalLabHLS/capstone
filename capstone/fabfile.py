import csv
import glob
import gzip
import hashlib
import os
from datetime import datetime
import django
import json
from random import randrange, randint
from pathlib import Path

# set up Django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
try:
    django.setup()
except Exception as e:
    print("WARNING: Can't configure Django -- tasks depending on Django will fail:\n%s" % e)

from django.conf import settings
from fabric.api import local
from fabric.decorators import task

from capapi.models import CapUser
from capdb.models import VolumeXML, VolumeMetadata, CaseXML, SlowQuery
import capdb.tasks as tasks
# from process_ingested_xml import fill_case_page_join_table
from scripts import set_up_postgres, ingest_tt_data, data_migrations, ingest_by_manifest, mass_update, \
    validate_private_volumes as validate_private_volumes_script, compare_alto_case, export


@task(alias='run')
def run_django(port="127.0.0.1:8000"):
    local("python manage.py runserver %s" % port)

@task
def test():
    """ Run tests with coverage report. """
    local("pytest --fail-on-template-vars --cov --cov-report=")

@task
def sync_with_s3():
    """ Import XML for volumes that have not had a previous completed import with that VolumeXML md5. """
    for volume_barcode in VolumeMetadata.objects.filter(ingest_status__in=['to_ingest', 'error']).values_list('barcode', flat=True):
        ingest_by_manifest.ingest_volume_from_s3.delay(volume_barcode)

@task
def total_sync_with_s3():
    """ Inspect and import any changed XML for all volumes, including those with previous successful import. """
    ingest_by_manifest.sync_s3_data.delay(full_sync=True)

@task
def validate_private_volumes():
    """ Confirm that all volmets files in private S3 bin match S3 inventory. """
    validate_private_volumes_script.validate_private_volumes()

@task
def ingest_jurisdiction():
    ingest_tt_data.populate_jurisdiction()

@task
def ingest_metadata():
    ingest_tt_data.populate_jurisdiction()
    ingest_tt_data.ingest(False)

@task
def sync_metadata():
    """
    Takes data from tracking tool db and translates them to the postgres db.
    Changes field names according to maps listed at the top of ingest_tt_data script.
    """
    ingest_tt_data.ingest(True)

@task
def relink_reporter_jurisdiction():
    """
    This will re-build the links between the Reporter table and Jurisdiction table
    """
    ingest_tt_data.relink_reporter_jurisdiction()

@task
def run_pending_migrations():
    data_migrations.run_pending_migrations()

@task
def update_postgres_env():
    set_up_postgres.update_postgres_env()

@task
def update_volume_metadata():
    """ Update VolumeMetadata fields from VolumeXML. """
    for volume_xml_id in VolumeXML.objects.values_list('pk', flat=True):
        tasks.update_volume_metadata.delay(volume_xml_id)

@task
def create_or_update_case_metadata(update_existing=False):
    """
        create or update CaseMetadata objects using celery
        - if update_existing, create and update
        - else, just create cases if missing
    """
    update_existing = True if update_existing else False
    tasks.create_case_metadata_from_all_vols(update_existing=update_existing)

@task
def rename_tags_from_json_id_list(json_path, tag=None):
    with open(os.path.abspath(os.path.expanduser(json_path))) as data_file:    
        parsed_json = json.load(data_file)
    mass_update.rename_casebody_tags_from_json_id_list(parsed_json, tag)

@task
def init_db():
    """
        Set up new dev database.
    """
    migrate()

    print("Creating DEV admin user:")
    CapUser.objects.create_superuser('admin@example.com', 'admin')

@task
def migrate():
    """
        Migrate all dbs at once
    """

    local("python manage.py migrate")

    if settings.USE_TEST_TRACKING_TOOL_DB:
        local("python manage.py migrate --database=tracking_tool")

    update_postgres_env()

@task
def load_test_data():
    if settings.USE_TEST_TRACKING_TOOL_DB:
        local("python manage.py loaddata --database=tracking_tool test_data/tracking_tool.json")
    ingest_metadata()
    ingest_jurisdiction()
    total_sync_with_s3()


@task
def add_permissions_groups():
    """
    Add permissions groups for admin panel
    """
    # add capapi groups
    local("python manage.py loaddata capapi/fixtures/groups.yaml")


@task
def validate_casemets_alto_link(sample_size=100000):
    """
    Will test a random sample of cases.
    Tests 100,000 by default, but you can specify a sample set size on the command line. For example, to test 14 cases:
    fab validate_casemets_alto_link:14
    """
    sample_size = int(sample_size) if int(sample_size) < CaseXML.objects.all().count() else CaseXML.objects.all().count()
    tested = []
    while len(tested) < sample_size:
        try:
            key = randrange(1, CaseXML.objects.last().id + 1)
            while key in tested:
                key = randrange(1, CaseXML.objects.last().id + 1)
            tested.append(key)
            case_xml = CaseXML.objects.get(pk=key)
            print(compare_alto_case.validate(case_xml))
        except CaseXML.DoesNotExist:
            continue
    print("Tested these CaseXML IDs:")
    print(tested)



@task
def add_test_case(*barcodes):
    """
        Write test data and fixtures for given volume and case. Example: fab add_test_case:32044057891608_0001

        NOTE:
            DATABASES['tracking_tool'] must point to real tracking tool db.
            STORAGES['ingest_storage'] must point to real harvard-ftl-shared.

        Output is stored in test_data/tracking_tool.json and test_data/from_vendor.
        Tracking tool user details are anonymized.
    """

    from django.core import serializers
    from tracking_tool.models import Volumes, Reporters, BookRequests, Pstep, Eventloggers, Hollis, Users
    from capdb.storages import ingest_storage
    from scripts.helpers import parse_xml, copy_file, resolve_namespace, serialize_xml

    ## write S3 files to local disk

    for barcode in barcodes:

        print("Writing data for", barcode)

        volume_barcode, case_number = barcode.split('_')

        # get volume dir
        source_volume_dirs = list(ingest_storage.iter_files(volume_barcode, partial_path=True))
        if not source_volume_dirs:
            print("ERROR: Can't find volume %s. Skipping!" % volume_barcode)
        source_volume_dir = sorted(source_volume_dirs, reverse=True)[0]

        # make local dir
        dest_volume_dir = os.path.join(settings.BASE_DIR, 'test_data/from_vendor/%s' % os.path.basename(source_volume_dir))
        os.makedirs(dest_volume_dir, exist_ok=True)

        # copy volume-level files
        for source_volume_path in ingest_storage.iter_files(source_volume_dir):
            dest_volume_path = os.path.join(dest_volume_dir, os.path.basename(source_volume_path))
            if '.' in os.path.basename(source_volume_path):
                # files
                copy_file(source_volume_path, dest_volume_path, from_storage=ingest_storage)
            else:
                # dirs
                os.makedirs(dest_volume_path, exist_ok=True)

        # read volmets xml
        source_volmets_path = glob.glob(os.path.join(dest_volume_dir, '*.xml'))[0]
        with open(source_volmets_path) as volmets_file:
            volmets_xml = parse_xml(volmets_file.read())

        # copy case file and read xml
        source_case_path = volmets_xml.find('mets|file[ID="casemets_%s"] > mets|FLocat' % case_number).attr(resolve_namespace('xlink|href'))
        source_case_path = os.path.join(source_volume_dir, source_case_path)
        dest_case_path = os.path.join(dest_volume_dir, source_case_path[len(source_volume_dir)+1:])
        copy_file(source_case_path, dest_case_path, from_storage=ingest_storage)
        with open(dest_case_path) as case_file:
            case_xml = parse_xml(case_file.read())

        # copy support files for case
        for flocat_el in case_xml.find('mets|FLocat'):
            source_path = os.path.normpath(os.path.join(os.path.dirname(source_case_path), flocat_el.attrib[resolve_namespace('xlink|href')]))
            dest_path = os.path.join(dest_volume_dir, source_path[len(source_volume_dir) + 1:])
            copy_file(source_path, dest_path, from_storage=ingest_storage)

        # remove unused files from volmets
        local_files = glob.glob(os.path.join(dest_volume_dir, '*/*'))
        local_files = [x[len(dest_volume_dir)+1:] for x in local_files]
        for flocat_el in volmets_xml.find('mets|FLocat'):
            if not flocat_el.attrib[resolve_namespace('xlink|href')] in local_files:
                file_el = flocat_el.getparent()
                file_el.getparent().remove(file_el)
        with open(source_volmets_path, "wb") as out_file:
            out_file.write(serialize_xml(volmets_xml))

    ## load metadata into JSON fixtures from tracking tool

    to_serialize = set()
    user_ids = set()
    volume_barcodes = [os.path.basename(d).split('_')[0] for d in
                glob.glob(os.path.join(settings.BASE_DIR, 'test_data/from_vendor/*'))]

    for volume_barcode in volume_barcodes:

        print("Updating metadata for", volume_barcode)

        try:
            tt_volume = Volumes.objects.get(bar_code=volume_barcode)
        except Volumes.DoesNotExist:
            raise Exception("Volume %s not found in the tracking tool -- is settings.py configured to point to live tracking tool data?" % volume_barcode)
        to_serialize.add(tt_volume)

        user_ids.add(tt_volume.created_by)

        tt_reporter = Reporters.objects.get(id=tt_volume.reporter_id)
        to_serialize.add(tt_reporter)

        to_serialize.update(Hollis.objects.filter(reporter_id=tt_reporter.id))

        request = BookRequests.objects.get(id=tt_volume.request_id)
        request.from_field = request.recipients = 'example@example.com'
        to_serialize.add(request)

        for event in Eventloggers.objects.filter(bar_code=tt_volume.bar_code):
            if not event.updated_at:
                event.updated_at = event.created_at
            to_serialize.add(event)
            user_ids.add(event.created_by)
            if event.pstep_id:
                pstep = Pstep.objects.get(step_id=event.pstep_id)
                to_serialize.add(pstep)

    for i, user in enumerate(Users.objects.filter(id__in=user_ids)):
        user.email = "example%s@example.com" % i
        user.password = 'password'
        user.remember_token = ''
        to_serialize.add(user)

    serializer = serializers.get_serializer("json")()
    with open(os.path.join(settings.BASE_DIR, "test_data/tracking_tool.json"), "w") as out:
        serializer.serialize(to_serialize, stream=out, indent=2)

    ## update inventory files
    write_inventory_files()


@task
def bag_jurisdiction(name, zip_directory=".", zip_filename=None):
    """
    Write a BagIt package of all case XML files in a given jurisdiction.
    """
    out_path = export.bag_jurisdiction(name, zip_directory, zip_filename)
    print("Exported jurisdiction %s to %s" % (name, out_path))


@task
def bag_reporter(name, zip_directory=".", zip_filename=None):
    """
    Write a BagIt package of all case XML files in a given reporter.
    """
    out_path = export.bag_reporter(name, zip_directory, zip_filename)
    print("Exported reporter %s to %s" % (name, out_path))


@task
def write_inventory_files(output_directory=os.path.join(settings.BASE_DIR, 'test_data/inventory/data')):
    """ Create inventory.csv.gz files in test_data/inventory/data. Should be re-run if test_data/from_vendor changes. """

    # get list of all files in test_data/from_vendor
    results = []
    for dir_name, subdir_list, file_list in os.walk(os.path.join(settings.BASE_DIR, 'test_data/from_vendor')):
        for file_path in file_list:
            if file_path == '.DS_Store':
                continue
            file_path = os.path.join(dir_name, file_path)

            # for each file, get list of [bucket, path, size, mod_time, md5, multipart_upload]
            results.append([
                'harvard-ftl-shared',
                file_path[len(os.path.join(settings.BASE_DIR, 'test_data/')):],
                os.path.getsize(file_path),
                datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                hashlib.md5(open(file_path, 'rb').read()).hexdigest(),
                'FALSE',
            ])

    # write results, split in half, to two inventory files named test_data/inventory/data/1.csv.gz and test_data/inventory/data/2.csv.gz
    for out_name, result_set in (("1", results[:len(results)//2]), ("2", results[len(results)//2:])):
        with gzip.open(os.path.join(output_directory, '%s.csv.gz' % out_name), "wt") as f:
            csv_w = csv.writer(f)
            for row in result_set:
                csv_w.writerow(row)

@task
def test_slow(jobs="1", ram="10", cpu="30"):
    """ For testing celery autoscaling, launch N jobs that will waste RAM and CPU. """
    from capdb.tasks import test_slow
    from celery import group
    import time

    print("Running %s test_slow jobs and waiting for results ..." % jobs)
    jobs = int(jobs)
    job = group(test_slow.s(i=i, ram=int(ram), cpu=int(cpu)) for i in range(jobs))
    start_time = time.time()
    result = job.apply_async()
    result.get()  # wait for all jobs to finish
    run_time = time.time() - start_time
    print("Ran %s test_slow jobs in %s seconds (%s seconds/job)" % (jobs, run_time, run_time/jobs))

@task
def fix_md5_columns():
    """ Run celery tasks to fix orig_xml and md5 column for all volumes. """
    for volume_id in VolumeXML.objects.values_list('pk', flat=True):
        tasks.fix_md5_column.delay(volume_id)

@task
def show_slow_queries():
    """
    Show slow queries for consumption by Slack bot.
    This requires

        shared_preload_libraries = 'pg_stat_statements'

    in postgresql.conf, that

        CREATE EXTENSION pg_stat_statements;

    has been run for the capstone database, and that

        GRANT EXECUTE ON FUNCTION pg_stat_statements_reset() TO <user>;

    has been run for the capstone user.
    """
    cursor = django.db.connection.cursor()
    with open('../services/postgres/s1_pg_stat_statements_top_total.sql') as f:
        sql = f.read()
        cursor.execute(sql)
    try:
        rows = cursor.fetchall()
        heading = "*capstone slow query report for %s*" % datetime.now().strftime("%Y-%m-%d")
        queries = []
    except:
        print(json.dumps({'text': 'Could not get slow queries'}))
        return
    for row in rows:
        call_count, run_time, query = row[0], row[1], row[8]

        # fetch query from DB log and update last seen time
        saved_query, created = SlowQuery.objects.get_or_create(query=query)
        if not created:
            saved_query.save(update_fields=['last_seen'])

        queries.append({
            'fallback': saved_query.label or query,
            'title': "%d call%s, %.1f ms, %.1f ms/query" % (
                call_count, "" if call_count == 1 else "s", run_time, run_time/float(call_count)
            ),
            'text': saved_query.label or "```%s```" % query
        })
    print(json.dumps({'text': heading, 'attachments': queries}))
    cursor.execute("select pg_stat_statements_reset();")


@task
def create_fixtures_db_for_benchmarking():
    """
    In settings_dev mark TEST_SLOW_QUERIES as True
    """
    try:
        local('psql -c "CREATE DATABASE %s;"' % settings.TEST_SLOW_QUERIES_DB_NAME)
        init_db()
    except:
        # Exception is thrown if test db has already been created
        pass

    migrate()


@task
def create_case_fixtures_for_benchmarking(amount=50000, randomize_casemets=False):
    """
    Create an amount of case fixtures.
    This tasks assumes the existence of some casemet xmls in test_data

    Make sure that you have the necessary jurisdictions for this task.
    It might be a good idea to:
        1. point tracking_tool to prod (WARNING: you are dealing with prod data be very very careful!)
        2. go to ingest_tt_data.py's `ingest` method and comment out all
            copyModel statements except
            `copyModel(Reporters, Reporter, reporter_field_map, dupcheck)`
            since Reporter is required for populating jurisdictions

        3. run `fab ingest_metadata`
        4. remove pointer to prod tracking_tool db!!
    """
    from test_data.test_fixtures.factories import CaseXMLFactory
    if randomize_casemets:
        # get all casemet paths to choose a random casemet xml
        # for test case creation
        casemet_paths = []
        d = os.path.join(settings.BASE_DIR, "test_data/from_vendor/")
        for root, dirs, files in os.walk(d):
            for name in files:
                if "_redacted_CASEMETS" in name:
                    casemet_paths.append(os.path.join(root, name))
        amount_of_paths = len(casemet_paths) - 1
    else:
        case_xml = (Path(settings.BASE_DIR) / "test_data/from_vendor/32044057892259_redacted/casemets/32044057892259_redacted_CASEMETS_0001.xml").read_text()

    for _ in range(amount):
        if randomize_casemets:
            case_xml = (Path(casemet_paths[randint(0, amount_of_paths)])).read_text()
        try:
            # create casexml and casemetadata objects, save to db
            CaseXMLFactory(orig_xml=case_xml)
        except:
            # Exception could happen because of duplicate slug keys on jurisdiction creation
            # For now, skipping this issue
            pass

@task
def tear_down_case_fixtures_for_benchmarking():
    """
    Make sure to mark settings_dev.TEST_SLOW_QUERIES as False
    """
    local('psql -c "DROP DATABASE %s;"' % settings.TEST_SLOW_QUERIES_DB_NAME)
