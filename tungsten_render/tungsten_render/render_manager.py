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
from urllib.error import HTTPError, URLError
import datetime
from pprint import pprint
from io import BytesIO
from decimal import Decimal

import pytz
import requests

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
    MODES = ["BENCHMARK", "RENDER"]

    def __init__(self):
        self.mode = "RENDER"
        if not ("AWS_ACCESS_KEY_ID" in os.environ and "AWS_SECRET_ACCESS_KEY" in os.environ):
            # try to decrypt AWS credentials via key in userdata
            for line in get_instance_userdata().splitlines():
                if "AWSSECRET=" in line:
                    secret = line.strip().split("=")[1]
                if "MODE=" in line:
                    mode = line.strip().split("=")[1]
                    if mode in self.MODES:
                        self.mode = mode
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
            if since_last_render.total_seconds() > 600: # 10 minutes 
                remainder = running.total_seconds() % 3600.0
                print("Remainder: %r" % remainder)
                if remainder > 2700:
                    print("Need to shut down this instance!")
                    iid = self.instance_metadata['instance-id']
                    self.sns_conn.publish("arn:aws:sns:us-east-1:324620253032:tungsten-ec2", subject="Shutting down " + iid, 
                        message="Shutting down this instance, since it's not processed a job in a while")
                    self.ec2_conn.terminate_instances(instance_ids=[iid])
                    return True
            time.sleep(15)

    def benchmark(self):
        pass


    def run(self):
        if self.mode == "BENCHMARK":
            return self.benchmark()

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
                _id = data['id']
                #if _id == 1:
                #    self.q.delete_message(msg)
                #    continue
                uid = data['uid']

                aws_s3_root = "https://s3.amazonaws.com/tungsten-render/" + uid
                do_root = "http://do.em32.net:8081/render/" + str(_id)

                spp = data.get('spp', 64)
                thumb = data.get('thumb')
                uploaded_scene_doc = data.get("scene_doc")
                commit = data.get('commit')
                resolution = data.get('resolution')
                resubmit = data.get('resubmit')

                requests.put(do_root, json={"status": "building"})


                if commit is not None and commit != "ANY":
                    if commit == 'LATEST':
                        commit = self.mgr.get_latest_commit()
                    try:
                        self.mgr.build(commit)
                        exe = self.mgr.get_exec(commit)
                        if exe is None:
                            raise Exception("Failed to build")
                    except Exception as ex:
                        requests.put(do_root, json={"status": "errored",
                            "commit": commit,
                            "err_msg": repr(ex.args)})

                        self.q.delete_message(msg)
                        print("Error building, won't render")
                        continue
                else:
                    commit = self.mgr.get_latest_built_commit()
                    exe = self.mgr.get_exec(commit)
                    if exe is None:
                        requests.put(do_root, json={"status": "errored",
                            "commit": commit,
                            "err_msg": "Unable to find a weorking build of tungsten"})
                        self.q.delete_message(msg)
                        print("Error building, won't render")
                        continue


                requests.put(do_root, json={"status": "rendering",
                    "commit": commit})


                try:
                    work_dir = tempfile.mkdtemp(prefix="tungsten")
                    print("work_dir=%r" % work_dir)
                    
                    downloaded_scene_zip = os.path.join(work_dir, "scene.zip")

                    # fetch the scene archive
                    if not uploaded_scene_doc:
                        uploaded_scene_doc = aws_s3_root + ".zip"
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
                    if True:
                        output_file = os.path.join(work_dir, "thumb.png")
                        scene_data['renderer']['output_file'] = output_file
                        scene_data['renderer']['overwrite_output_files'] = True
                        scene_data['renderer']['spp'] = 64

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
                        
                        requests.put(do_root, json={"thumb_url": aws_s3_root + ".thumb.png"})


                    if resubmit:
                        old_resume_dat = "https://s3.amazonaws.com/tungsten-render/" + resubmit['uid'] + ".resume.dat"
                        old_resume_json = "https://s3.amazonaws.com/tungsten-render/" + resubmit['uid'] + ".resume.json"
                        resume_dat = os.path.join(work_dir, uid + ".resume.dat")
                        resume_json = os.path.join(work_dir, uid + ".resume.json")
                        try:
                            handle = urlopen(old_resume_dat)
                            shutil.copyfileobj(handle, open(resume_dat, "w+b"))
                            handle = urlopen(old_resume_json)
                            shutil.copyfileobj(handle, open(resume_json, "w+b"))
                            print("Successfully downloaded resume info")
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
                    print("tungsten_server started with pid %d" % p.pid)


                    render_start_time = time.time()
                    time.sleep(1)
                    last_status_check = 0
                    last_framegrab_time = render_start_time
                    last_checkpoint_time = render_start_time
                    start_spp = None
                    framegrab_interval = 30
                    while p.poll() is None:
                        time.sleep(5)
                        now = time.time()
                        try:
                            if now - last_status_check > 15.0:
                                render_status = requests.get("http://localhost:12345/status").json()
                                if start_spp is None:
                                    start_spp = render_status['current_spp']
                                    continue
                                rate = float(render_status['current_spp'] - start_spp) / (now - render_start_time) # in samples per second
                                if rate > 0.0:
                                    est_done_time = render_start_time + (float(render_status['total_spp'] - start_spp) / rate)
                                    reply = requests.put(do_root, json={"render_status": render_status, "spp_rate": rate,
                                        "est_done_time": est_done_time, "start_spp": start_spp}).json()
                                    if reply and reply.get("requested_action") == "stop":
                                        p.kill()
                                        requests.put(do_root, json={"status": "errored",
                                            "err_msg": "Cancel requested by user"})
                                last_status_check = now
                            if now - last_framegrab_time > framegrab_interval:
                                print("Uploading frame to do_root")
                                requests.post(do_root + "/preview.png", files={"file": urlopen("http://localhost:12345/render")})
                                last_framegrab_time = now
                                framegrab_interval = min(framegrab_interval*1.4, 300)
                                print("Uploaded frame to do_root")
                            if False and now - last_checkpoint_time > 600:
                                if os.path.exists(restart_dat) and os.path.exists(restart_json):
                                    self.upload_to_s3(uid + ".resume.dat", file_name=restart_dat)
                                    self.upload_to_s3(uid + ".resume.json", file_name=restart_json)
                                    print("Uploaded resume data")
                                last_checkpoint_time = now
                        except Exception as ex:
                            traceback.print_exc()
                            # we might fail to urlopen if the render has finished
                            time.sleep(1)
                            if p.poll() is None: # still running? this is a real error
                                print("renderer is still running! but had HTTPError")
                                raise ex


                    if p.wait() != 0:
                        print("Bad error code! %r" % p.returncode)

                    requests.put(do_root, json={"status": "uploading"})

                    if os.path.exists(output_file):
                        print("Uploading final scene to S3")
                        self.upload_to_s3(uid + ".png", file_name=output_file)
                    else:
                        requests.put(do_root, json={"status": "errored",
                            "err_msg": open(logfile).read()})
                        continue


                    if os.path.exists(restart_dat) and os.path.exists(restart_json):
                        print("Uploading resume data to S3")
                        self.upload_to_s3(uid + ".resume.dat", file_name=restart_dat)
                        self.upload_to_s3(uid + ".resume.json", file_name=restart_json)



                    shutil.rmtree(work_dir)
                    requests.put(do_root, json={"status": "finished", "final_render_url": aws_s3_root + ".png"})
                    print("Render is done!  Waiting for next item")
                except Exception:
                    traceback.print_exc()
                    requests.put(do_root, json={"status": "errored",
                            "err_msg": traceback.format_exc()})

                self.last_render = datetime.datetime.now(pytz.utc)
            if self.sleep():
                break

        print("Done working!")

if __name__ == "__main__":
    mgr = RenderManager()
    mgr.run()
