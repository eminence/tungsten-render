from django.shortcuts import render
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.core.urlresolvers import reverse
from django.core.files.uploadhandler import TemporaryFileUploadHandler
from django.template import RequestContext, loader
from django.shortcuts import render

import json
import uuid
import zipfile
import time
from datetime import datetime
import pygit2

# Create your views here.
import boto
import boto.sqs
from boto.sqs.message import Message
from boto.s3.key import Key
from boto.dynamodb2.table import Table
from boto.dynamodb2.exceptions import ItemNotFound

sqs_conn = boto.sqs.connect_to_region("us-east-1")
s3_conn = boto.connect_s3()

sqs_q = sqs_conn.get_queue("tungsten_render")
s3_bucket = s3_conn.get_bucket("tungsten-render")
table = Table("tungsten-render")

TUNGSTEN_CLONE_DIR = "/tmp/tungsten_clone"
TUNGSTEN_REPO = "https://github.com/tunabrain/tungsten.git"

try:
    repo = pygit2.Repository(TUNGSTEN_CLONE_DIR)
except Exception:
    repo = pygit2.clone_repository(TUNGSTEN_REPO, TUNGSTEN_CLONE_DIR)


last_git_fetch_time = 0


@csrf_exempt
def post_render(request):
    global last_git_fetch_time
    if request.method == "GET":
        if time.time() - last_git_fetch_time > 120:
            t = repo.remotes['origin'].fetch()
            while (t.received_objects < t.total_objects):
                time.sleep(1)
            last_git_fetch_time = time.time()
        print("Fetched latest code from github")

        latest = repo.lookup_branch("origin/master", pygit2.GIT_BRANCH_REMOTE).target
        commits = [x for i,x in enumerate(repo.walk(latest, pygit2.GIT_SORT_TOPOLOGICAL)) if i < 20]

        resubmit = request.GET.get("resubmit")

        context = {
                "navpage": "render",
                "commits": commits,
                "resubmit": resubmit}

        if resubmit:
            old_item = table.get_item(uid=resubmit)
            context['old_item'] = old_item
        return render(request, "render.html", context)

    if request.method == "POST":
        request.upload_handlers = [TemporaryFileUploadHandler()]
        
        # create a new UUID to track this render
        uid = uuid.uuid4()

        resubmit = request.GET.get("resubmit")
        if not resubmit:
            tmp_file = request.FILES['data'].temporary_file_path()
            print("tmp_file is %r" % tmp_file)
            # verify the document can be unzip
            try:
                z = zipfile.ZipFile(tmp_file)
            except IOError:
                return HttpResponseBadRequest()

            # upload json document to s3
            key = Key(s3_bucket)
            key.key = uid.hex + ".zip"
            key.set_contents_from_filename(tmp_file)
            key.set_acl('public-read')

            scene_doc = "https://s3.amazonaws.com/tungsten-render/" + uid.hex + ".zip"
        else:
            old_item = table.get_item(uid=resubmit)
            scene_doc = old_item['message']['scene_doc']
        

        rendername = request.POST.get("name")
        spp = request.POST.get("spp")
        thumb = bool(request.POST.get("thumb"))
        resolution = request.POST.get("resolution")
        commit = request.POST.get("commit")
    
        # the user can customze the spp value via queryargs
    
        # add job to queue
        m = Message()
        body = {"uid": uid.hex,"thumb": thumb, "commit": commit,
            "scene_doc": scene_doc}
        if spp:
            body['spp'] = min(256, int(spp))
        else:
            body['spp'] = 256
        if rendername:
            body['name'] = rendername
        if resolution:
            try:
                resolution = map(int, resolution.split("x"))
                body['resolution'] = [resolution[0], resolution[1]]
            except IndexError:
                pass
        m.set_body(json.dumps(body))
        sqs_q.write(m)

        table.put_item(data={
            "uid":uid.hex,
            "status":"submitted",
            "created":time.time(),
            "message": body
            })



    resp = JsonResponse({"status": "queued", "uid": uid.hex})
    resp['Location'] = reverse("get_render_status", kwargs={"uid": uid.hex})
    return resp
    #return HttpResponseRedirect(reverse("get_render_status", kwargs={"uid": uid.hex}))

def get_render(request, uid):
    return HttpResponseRedirect("https://s3.amazonaws.com/tungsten-render/" + uid + ".png")

def get_thumb(request, uid):
    return HttpResponseRedirect("https://s3.amazonaws.com/tungsten-render/" + uid + ".thumb.png")

def get_scene_doc(request, uid):
    pass

def get_render_status(request, uid):
    try:
        item = table.get_item(uid=uid)
    except ItemNotFound:
        item = None
    context = {"item": item}
    if item is None:
        return render(request, "status.html", context)
    d = {
        "navpage": "status",
        "created": item['created'],
        "status": item['status'],
        }
    d['thumb'] = "https://s3.amazonaws.com/tungsten-render/" + uid + ".thumb.png"
    d['render'] = "https://s3.amazonaws.com/tungsten-render/" + uid + ".png"

    return render(request, "status.html", context)


def recent_renders(request):
    week = 60*60*24
    renders = table.scan(created__gt=int(time.time()-week))
    renders = sorted(renders, key=lambda x:x['created'], reverse=True)

    context = {"renders": renders,
            "navpage": "recent"} 
    return render(request, "recent.html", context)
