#!/usr/bin/env python

import time
import json
import tempfile
import os
import zipfile
import subprocess
import uuid
import shutil
import traceback
from urllib.request import urlopen

import boto
import boto.sqs
from pprint import pprint
from io import BytesIO

from boto.s3.key import Key
from boto.dynamodb2.table import Table
from boto.dynamodb2.exceptions import ItemNotFound


from manager_settings import *
from build_manager import BuildManager

sqs_conn = boto.sqs.connect_to_region("us-east-1")
s3_conn = boto.connect_s3()

q = sqs_conn.get_queue("tungsten_render")
s3_bucket = s3_conn.get_bucket("tungsten-render")

table = Table("tungsten-render")

def upload_to_s3(key, file_name):
    pngkey = Key(s3_bucket)
    pngkey.key = key
    pngkey.set_contents_from_filename(file_name)
    pngkey.set_acl('public-read')

mgr = BuildManager(TUNGSTEN_BUILD_DIR)
try:
    mgr.build()
except Exception:
    print("Waring! Latest version of tungsten failed to build")



print("Waiting for a job...")
while True:
    msgs = q.get_messages(num_messages=1)
    if msgs:
        msg = msgs[0]
        try:
            data = json.loads(msg.get_body())
        except ValueError:
            print("Error decoding json")
            q.delete_message(msg)
            continue
        print("Have message: %r" % data)
        uid = uuid.UUID(data['uid']).hex
        #if uid == '7096c1416b194898bb7b0834eb94951a':
        #    q.delete_message(msg)
        #    continue


        spp = data.get('spp', 64)
        thumb = data.get('thumb')
        uploaded_scene_doc = data.get("scene_doc")
        commit = data.get('commit')
        try:
            item = table.get_item(uid=uid)
        except ItemNotFound:
            item = None
        while item is None:
            print("Waiting for database entry")
            time.sleep(1)
            try:
                item = table.get_item(uid=uid)
            except ItemNotFound:
                item = None

        item['status'] = 'building'
        item.partial_save()


        if commit is not None and commit != "ANY":
            if commit == 'LATEST':
                commit = mgr.get_latest_commit()
            try:
                mgr.build(commit)
                exe = mgr.get_exec(commit)
                if exe is None:
                    raise Exception("Failed to build")
            except Exception as ex:
                item['status'] = "error"
                item['commit'] = commit 
                item['err_msg'] = repr(ex.args)
                item.partial_save()
                q.delete_message(msg)
                print("Error building, won't render")
                continue
        else:
            commit = mgr.get_latest_built_commit()
            exe = mgr.get_exec(commit)
            if exe is None:
                item['status'] = "error"
                item['commit'] = commit 
                item['err_msg'] = "Unable to find a working build of tungsten"
                item.partial_save()
                q.delete_message(msg)
                print("Error building, won't render")
                continue



        item['commit'] = commit 
        item['status'] = 'inprogress'
        item.partial_save()

        try:
            work_dir = tempfile.mkdtemp(prefix="tungsten")
            print("work_dir=%r" % work_dir)
            
            downloaded_scene_zip = os.path.join(work_dir, "scene.zip")

            # fetch the scene archive
            if not uploaded_scene_doc:
                uploaded_scene_doc = "https://s3.amazonaws.com/tungsten-render/" + uid + ".zip"
            handle = urlopen(uploaded_scene_doc)
            shutil.copyfileobj(handle, open(downloaded_scene_zip, "w+b"))
            print("Downloaded scene document")

            subprocess.Popen(["unzip", "scene.zip"], cwd=work_dir).wait()

            try:
                for path in os.listdir(work_dir):
                    if path.endswith(".json"):
                        with open(os.path.join(work_dir, path)) as fobj:
                            scene_data = json.load(fobj)
                            break
            except ValueError:
                print("Error decoding scene document")
                q.delete_message(msg)
                continue

            # delete the message so no other worker will get it
            q.delete_message(msg)
                
            # if resolution is not specified:
            if "resolution" not in scene_data['camera']:
                res = [1000, 563] # the tungsten default
            else:
                res = scene_data['camera']['resolution']

            # if thumb is set, render a small thumbnail that less than 128px
            # with a fixed spp of 32
            if thumb:
                output_file = os.path.join(work_dir, "thumb.png")
                scene_data['camera']['output_file'] = output_file
                scene_data['camera']['overwrite_output_files'] = True
                scene_data['camera']['spp'] = 32

                factor = max(res)/128.0
                nres = [int(res[0]/factor), int(res[1]/factor)]
                scene_data['camera']['resolution'] = nres

                final_scene_file = os.path.join(work_dir, uid) + ".thumb.json"
                json.dump(scene_data, open(final_scene_file, "w"))
                
                scene_data['camera']['resolution'] = res # reset to original
                
                p = subprocess.Popen([exe, 
                    final_scene_file], cwd=work_dir)
                p.wait()

                upload_to_s3(uid + ".thumb.png", output_file)
                
                item['thumb'] = 'thumb.png'
                item.partial_save()


            output_file = os.path.join(work_dir, "output.png")

            # modify the scene document to specify our own output file
            scene_data['camera']['output_file'] = output_file
            scene_data['camera']['overwrite_output_files'] = True
            if spp is not None:
                scene_data['camera']['spp'] = spp

            final_scene_file = os.path.join(work_dir, uid) + ".json"
            json.dump(scene_data, open(final_scene_file, "w"))

            p = subprocess.Popen([exe, 
                final_scene_file], cwd=work_dir)
            p.wait()

            upload_to_s3(uid + ".png", output_file)

            shutil.rmtree(work_dir)
            item['status'] = 'done'
            item['finished'] = time.time()
            item.partial_save()
            print("Result uploaded and work_dir deleted")
        except Exception:
            traceback.print_exc()
            item['status'] = 'error'
            item['finished'] = time.time()
            item.partial_save()

    time.sleep(15)
