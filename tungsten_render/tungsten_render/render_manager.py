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
from urllib.error import HTTPError
import datetime
from pprint import pprint
from io import BytesIO

import pytz

import boto
import boto.sqs
import boto.ec2
import boto.sns
from boto.s3.key import Key
from boto.dynamodb2.table import Table
from boto.dynamodb2.exceptions import ItemNotFound
from boto.utils import get_instance_metadata, get_instance_userdata


from manager_settings import *
from build_manager import BuildManager

ENC_ACCESS_KEY = b'\x9a\x98\x18\x16P\xdetGu@\xcd\xfb\xd1Iv\x83\x89\x8b?\x1d\xf3\xdd\x96M\xb9\x8b\xb5\x18\xba\xec\x99\x0f\xae\xeb\x84c'
ENC_SECRET_KEY = b'\xe9\xc5KJ\x94\xe7\x92\xb5`U\x1at\xf5\x8e\xd7~p\xf9\xa5\xc6\x98\x8bT\x0e*\xfe\x08\xe1\x86\x14\xa8\xaa\x83\x93\x8d\x98\x80\xa0\xf0\x88A\xf5G+(T\xa93T\x03\x87\xbd[x\xfc\x7f'

class RenderManager(object):

    def __init__(self):
        if not ("AWS_ACCESS_KEY_ID" in os.environ and "AWS_SECRET_ACCESS_KEY" in os.environ):
            # try to decrypt AWS credentials via key in userdata
            for line in get_instance_userdata().splitlines():
                if "AWSSECRET=" in line:
                    secret = line.strip().split("=")[1]
                    break
            else:
                raise Exception("Please set your AWS credentials!")
            from Crypto.Cipher import AES
            from Crypto import Random
            from hashlib import md5

            key = md5(secret.encode()).digest()
            iv = ENC_ACCESS_KEY[0:AES.block_size]
            cipher = AES.new(key, AES.MODE_CFB, iv)
            os.environ['AWS_ACCESS_KEY_ID'] = cipher.decrypt(ENC_ACCESS_KEY[AES.block_size:]).decode()
            
            iv = ENC_SECRET_KEY[0:AES.block_size]
            cipher = AES.new(key, AES.MODE_CFB, iv)
            os.environ['AWS_SECRET_ACCESS_KEY'] = cipher.decrypt(ENC_SECRET_KEY[AES.block_size:]).decode()
            
            

        self.sqs_conn = boto.sqs.connect_to_region("us-east-1")
        self.s3_conn = boto.connect_s3()
        self.ec2_conn = boto.ec2.connect_to_region("us-east-1")
        self.sns_conn = boto.sns.connect_to_region("us-east-1")

        self.q = self.sqs_conn.get_queue("tungsten_render")
        self.s3_bucket = self.s3_conn.get_bucket("tungsten-render")

        self.table = Table("tungsten-render")

        self.instance_metadata = get_instance_metadata(timeout=5, num_retries=1)
        self.is_ec2 = bool(self.instance_metadata)

        self.mgr = BuildManager(TUNGSTEN_BUILD_DIR)

        if self.is_ec2:
            print("Running under EC2!")
            self.instance_data = self.ec2_conn.get_only_instances(self.instance_metadata['instance-id'])[0]
            instance_launch_time = datetime.datetime.strptime(self.instance_data.launch_time, "%Y-%m-%dT%H:%M:%S.%fZ")
            self.instance_launch_time = pytz.utc.localize(instance_launch_time)

        else:
            print("not running under EC2, automatic shutdown functionality will be disabled")
        
        try:
            self.mgr.build()
        except Exception:
            print("Waring! Latest version of tungsten failed to build")

        self.last_render = datetime.datetime.now(pytz.utc)

    def upload_to_s3(self, key, file_name=None, file=None):
        if file_name is None and file is None:
            raise Exception("")
        if file_name is not None:
            if not os.path.exists(file_name):
                raise Exception("File not found!")
        pngkey = Key(self.s3_bucket)
        pngkey.key = key
        if file_name is not None:
            pngkey.set_contents_from_filename(file_name)
        else:
            pngkey.set_contents_from_file(file)

        pngkey.set_acl('public-read')

    def get_db_item(self, uid):
        try:
            item = self.table.get_item(uid=uid)
        except ItemNotFound:
            item = None
        while item is None:
            print("Waiting for database entry")
            time.sleep(1)
            try:
                item = self.table.get_item(uid=uid)
            except ItemNotFound:
                item = None
        return item

    def sleep(self):
        if not self.is_ec2:
            time.sleep(15)
        else:
            now = datetime.datetime.now(pytz.utc)
            running = now - self.instance_launch_time
            print("Running for %r seconds" % running.total_seconds())

            since_last_render = now - self.last_render
            print("It's been %r seconds since last render" % since_last_render.total_seconds()) 
            if since_last_render.total_seconds() > 1500: # 25 minutes 
                remainder = running.total_seconds() % 3600.0
                print("Remainder: %r" % remainder)
                if remainder > 2700:
                    print("Need to shut down this instance!")
                    iid = self.instance_metadata['instance-id']
                    self.sns_conn.publish("arn:aws:sns:us-east-1:324620253032:tungsten-ec2", subject="Shutting down " + iid, 
                        message="Shutting down this instance, since it's not processed a job in a while")
                    self.ec2_conn.stop_instances(instance_ids=[iid])
                    return True
            time.sleep(15)

    def run(self):

        print("Waiting for a job...")
        while True:
            msgs = self.q.get_messages(num_messages=1)
            if msgs:
                self.last_render = datetime.datetime.now(pytz.utc)
                msg = msgs[0]
                try:
                    data = json.loads(msg.get_body())
                except ValueError:
                    print("Error decoding json")
                    self.q.delete_message(msg)
                    continue
                print("Have message: %r" % data)
                uid = uuid.UUID(data['uid']).hex
                #if uid == '7096c1416b194898bb7b0834eb94951a':
                #    self.q.delete_message(msg)
                #    continue

                item = self.get_db_item(uid)

                spp = data.get('spp', 64)
                thumb = data.get('thumb')
                uploaded_scene_doc = data.get("scene_doc")
                commit = data.get('commit')
                resolution = data.get('resolution')
                resubmit = data.get('resubmit')

                item['status'] = 'building'
                item.partial_save()


                if commit is not None and commit != "ANY":
                    if commit == 'LATEST':
                        commit = self.mgr.get_latest_commit()
                    try:
                        self.mgr.build(commit)
                        exe = self.mgr.get_exec(commit)
                        if exe is None:
                            raise Exception("Failed to build")
                    except Exception as ex:
                        item['status'] = "error"
                        item['commit'] = commit 
                        item['err_msg'] = repr(ex.args)
                        item.partial_save()
                        self.q.delete_message(msg)
                        print("Error building, won't render")
                        continue
                else:
                    commit = self.mgr.get_latest_built_commit()
                    exe = self.mgr.get_exec(commit)
                    if exe is None:
                        item['status'] = "error"
                        item['commit'] = commit 
                        item['err_msg'] = "Unable to find a working build of tungsten"
                        item.partial_save()
                        self.q.delete_message(msg)
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
                        self.q.delete_message(msg)
                        continue

                    # delete the message so no other worker will get it
                    self.q.delete_message(msg)
                    
                    if resolution is not None:
                        scene_data['camera']['resolution'] = resolution


                    # if resolution is not specified:
                    if "resolution" not in scene_data['camera']:
                        res = [1000, 563] # the tungsten default
                    else:
                        res = scene_data['camera']['resolution']

                    # if thumb is set, render a small thumbnail that less than 128px
                    # with a fixed spp of 32
                    # note thumbnails never use the resume info
                    if thumb:
                        output_file = os.path.join(work_dir, "thumb.png")
                        scene_data['renderer']['output_file'] = output_file
                        scene_data['renderer']['overwrite_output_files'] = True
                        scene_data['renderer']['spp'] = 32

                        factor = max(res)/128.0
                        nres = [int(res[0]/factor), int(res[1]/factor)]
                        scene_data['camera']['resolution'] = nres

                        final_scene_file = os.path.join(work_dir, uid) + ".thumb.json"
                        json.dump(scene_data, open(final_scene_file, "w"))
                        
                        scene_data['camera']['resolution'] = res # reset to original
                        
                        p = subprocess.Popen([exe, "--restart",
                            final_scene_file], cwd=work_dir)
                        p.wait()

                        self.upload_to_s3(uid + ".thumb.png", file_name=output_file)
                        
                        item['thumb'] = 'thumb.png'
                        item.partial_save()


                    if resubmit:
                        old_resume_dat = "https://s3.amazonaws.com/tungsten-render/" + resubmit + ".resume.dat"
                        old_resume_json = "https://s3.amazonaws.com/tungsten-render/" + resubmit + ".resume.json"
                        resume_dat = os.path.join(work_dir, uid + ".resume.dat")
                        resume_json = os.path.join(work_dir, uid + ".resume.json")
                        try:
                            handle = urlopen(old_resume_dat)
                            shutil.copyfileobj(handle, open(resume_dat, "w+b"))
                            handle = urlopen(old_resume_json)
                            shutil.copyfileobj(handle, open(resume_json, "w+b"))
                        except HTTPError:
                            print("Failed to download resume information!")

                    output_file = os.path.join(work_dir, "output.png")

                    # modify the scene document to specify our own output file
                    scene_data['renderer']['output_file'] = output_file
                    scene_data['renderer']['overwrite_output_files'] = True
                    scene_data['renderer']['enable_resume_render'] = True
                    scene_data['renderer']['resume_render_prefix'] = uid + ".resume"
                    scene_data['renderer']['checkpoint_interval'] = 5
                    if spp is not None:
                        scene_data['renderer']['spp'] = spp

                    final_scene_file = os.path.join(work_dir, uid) + ".json"
                    json.dump(scene_data, open(final_scene_file, "w"))
                    logfile = os.path.join(work_dir, "render.log")

                    restart_dat = os.path.join(work_dir, uid + ".resume.dat")
                    restart_json = os.path.join(work_dir, uid + ".resume.json")

                    p = subprocess.Popen([exe+"_server", "-p", "12345", "-l", logfile,
                        final_scene_file], cwd=work_dir)
                    render_start_time = time.time()
                    last_status_check = render_start_time
                    last_framegrab_time = render_start_time
                    last_checkpoint_time = render_start_time
                    while p.poll() is None:
                        time.sleep(5)
                        now = time.time()
                        if now - last_status_check > 30.0:
                            render_status = json.loads(urlopen("http://localhost:12345/status").read().decode())
                            item["render_status"] = render_status
                            item.partial_save()
                            last_status_check = now
                        if now - last_framegrab_time > 180:
                            with open(os.path.join(work_dir, "preview.png"), "wb") as fobj:
                                shutil.copyfileobj(urlopen("http://localhost:12345/render"), fobj)
                            self.upload_to_s3(uid + ".preview.png", file_name=os.path.join(work_dir, "preview.png"))
                            item["preview"] = "preview.png"
                            item.partial_save()
                            last_framegrab_time = now
                            print("Uploaded frame to s3")
                        if now - last_checkpoint_time > 360:
                            if os.path.exists(restart_dat) and os.path.exists(restart_json):
                                self.upload_to_s3(uid + ".resume.dat", file_name=restart_dat)
                                self.upload_to_s3(uid + ".resume.json", file_name=restart_json)
                                print("Uploaded resume data")
                            last_checkpoint_time = now


                    p.wait()

                    if os.path.exists(output_file):
                        self.upload_to_s3(uid + ".png", file_name=output_file)
                    else:
                        item['status'] = 'error'
                        item['err_msg'] = open(logfile).read()
                        item.partial_save()
                        continue


                    if os.path.exists(restart_dat) and os.path.exists(restart_json):
                        self.upload_to_s3(uid + ".resume.dat", file_name=restart_dat)
                        self.upload_to_s3(uid + ".resume.json", file_name=restart_json)



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

                self.last_render = datetime.datetime.now(pytz.utc)
            if self.sleep():
                break

        print("Done working!")

if __name__ == "__main__":
    mgr = RenderManager()
    mgr.run()
