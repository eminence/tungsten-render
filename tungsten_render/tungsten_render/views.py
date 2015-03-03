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
import pytz

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
        commits = []
        for i,commit in enumerate(repo.walk(latest, pygit2.GIT_SORT_TOPOLOGICAL)):
            if i >= 20:
                break
            if commit.oid.hex == '0bdb297ef067f782130793682e2a255b4e61f639':
                commits.append(commit)
                break
            commits.append(commit)

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
        if resubmit:
            body['resubmit'] = resubmit
        if spp:
            body['spp'] = min(2048, int(spp))
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



    return HttpResponseRedirect(reverse("get_render_status", kwargs={"uid": uid.hex}))
    resp = JsonResponse({"status": "queued", "uid": uid.hex})
    resp['Location'] = reverse("get_render_status", kwargs={"uid": uid.hex})
    return resp

def bootstrap(request):
    "A script that will run the very first time an EC2 instances is started"
    script="""#!/bin/bash
apt-get update
apt-get -q -y install vim tmux python3 python3-dev git cmake make g++ gcc libffi-dev libssl-dev


update-rc.d -f tungsten remove

curl http://do.em32.net:8081/render/startup.sh > /etc/init.d/tungsten
chmod +x /etc/init.d/tungsten

update-rc.d tungsten start 99 5 . stop 99 1 2 3 4 6 .
/etc/init.d/tungsten start


"""
    return HttpResponse(script)

def startup(request):
    "A script that will be installed by bootstrap.sh to be run at every boot"
    script="""#!/bin/bash

#!/bin/sh

### BEGIN INIT INFO
# Provides:          screen-cleanup
# Required-Start:    $remote_fs
# Required-Stop:     $remote_fs
# Default-Start:     5
# Default-Stop:      1 2 3 4 6
# Short-Description: Tungsten render service
# Description: Tungsten render service
### END INIT INFO


case "$1" in
start)
    sudo -u ubuntu tmux new-session -d '/etc/init.d/tungsten render'
    ;;
render)
    cd
    if ! [ -e tungsten-render ]; then
        git clone https://github.com/eminence/tungsten-render.git
    fi
    cd tungsten-render
    git submodule init
    git submodule update --depth 1
    if ! [ -e venv ]; then
        python3 deps/virtualenv/virtualenv.py venv
    fi
    source venv/bin/activate
    ./deps/build_deps.sh
    python tungsten_render/tungsten_render/render_manager.py
    echo "Dropping into bash shell"
    bash


    ;;
stop|restart|reload|force-reload)
    ;;
esac
        

"""
    return HttpResponse(script)

def get_render(request, uid):
    return HttpResponseRedirect("https://s3.amazonaws.com/tungsten-render/" + uid + ".png")

def get_thumb(request, uid):
    return HttpResponseRedirect("https://s3.amazonaws.com/tungsten-render/" + uid + ".thumb.png")

def get_preview(request, uid):
    return HttpResponseRedirect("https://s3.amazonaws.com/tungsten-render/" + uid + ".preview.png")

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
        "item": item,
        }
    d['thumb'] = "https://s3.amazonaws.com/tungsten-render/" + uid + ".thumb.png"
    d['render'] = "https://s3.amazonaws.com/tungsten-render/" + uid + ".png"
    if 'spprate' in item:
        d['progress'] = int(100.0 * float(item['render_status']['current_spp']) / float(item['render_status']['total_spp']))
        d['spprate'] = item['spprate']
        d['eta'] = pytz.utc.localize(datetime.utcfromtimestamp(float(item['est_done_time'])))
        d['now'] = pytz.utc.localize(datetime.utcnow())

        

    return render(request, "status.html", d)


def recent_renders(request):
    week = 60*60*24*7
    renders = table.scan(created__gt=int(time.time()-week))
    renders = sorted(renders, key=lambda x:x['created'], reverse=True)

    context = {"renders": renders,
            "navpage": "recent"} 
    return render(request, "recent.html", context)
