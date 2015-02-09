from django.shortcuts import render
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.urlresolvers import reverse

import json
import uuid

# Create your views here.
import boto
import boto.sqs
from boto.sqs.message import Message
from boto.s3.key import Key

sqs_conn = boto.sqs.connect_to_region("us-east-1")
s3_conn = boto.connect_s3()

sqs_q = sqs_conn.get_queue("tungsten_render")
s3_bucket = s3_conn.get_bucket("tungsten-render")


@csrf_exempt
def post_render(request):
    if request.method == "POST":
        size = int(request.META['CONTENT_LENGTH'])
        data = request.read(size)

        #verify the document is real json
        try:
            doc = json.loads(data)
        except ValueError:
            return HttpResponseBadRequest()

        # create a new UUID to track this render
        uid = uuid.uuid4()

        # upload json document to s3
        key = Key(s3_bucket)
        key.key = uid.get_hex() + ".json"
        key.set_contents_from_string(data)
        key.set_acl('public-read')

        # add job to queue
        m = Message()
        m.set_body(json.dumps({"scene": uid.get_hex()}))
        sqs_q.write(m)



    resp = JsonResponse({"status": "queued", "uid": uid.get_hex()})
    resp['Location'] = reverse("get_render", kwargs={"uid": uid.get_hex()})
    return resp

def get_render(request, uid):
    pass

def get_scene_doc(request, uid):
    pass
