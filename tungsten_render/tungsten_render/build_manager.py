from manager_settings import *

import os
import pygit2
import time
import subprocess
import shutil
import multiprocessing

class BuildManager(object):
    def __init__(self, build_dir):
        if not os.path.exists(build_dir):
            os.mkdir(build_dir)

        try:
            self.repo = pygit2.Repository(TUNGSTEN_CLONE_DIR)
        except Exception:
            self.repo = pygit2.clone_repository(TUNGSTEN_REPO, TUNGSTEN_CLONE_DIR)

        self.builds = {}
        for path in os.listdir(build_dir):
            success_dir = os.path.join(build_dir, path, "build.success")
            tungsten = os.path.join(build_dir, path, "build", "release", "tungsten")
            if os.path.exists(success_dir):
                # confirm this is a commit
                if self.repo.get(path) is None:
                    continue
                    
                self.builds[path] = {"exec": tungsten, "success": True}

    def get_latest_commits(self, num):
        latest = self.repo.lookup_branch("origin/master", pygit2.GIT_BRANCH_REMOTE).target
        count = 1
        for commit in self.repo.walk(latest, pygit2.GIT_SORT_TOPOLOGICAL):
            yield commit.oid.hex
            count += 1
            if count > num:
                return

    def get_latest_commit(self):
        self.fetch()
        return self.repo.lookup_branch("origin/master", pygit2.GIT_BRANCH_REMOTE).target.hex


    def get_latest_built_commit(self):
        for commit in self.get_latest_commits(10):
            ex = self.get_exec(commit)
            if ex: 
                return ex

    def get_exec(self, commit):
        if commit not in self.builds:
            return None
        if not self.builds[commit].get('success'):
            return None
        return self.builds[commit]['exec']

    def fetch(self):
        t = self.repo.remotes['origin'].fetch()
        while (t.received_objects < t.total_objects):
            time.sleep(1)

    def build(self, commit=None, force=False):
        self.fetch()

        if commit is None: # build latest
            commit = self.get_latest_commit()

        git_commit = self.repo.get(commit)
        if git_commit is None:
            raise ValueError("Not a valid commit")
        if git_commit.type != pygit2.GIT_OBJ_COMMIT:
            raise ValueError("Object is not a commit!")

        self.repo.reset(commit, pygit2.GIT_RESET_HARD)

        build_root = os.path.join(TUNGSTEN_BUILD_DIR, commit)
        lock_dir = os.path.join(build_root, "build.lock")
        success_dir = os.path.join(build_root, "build.success")
        fail_dir = os.path.join(build_root, "build.fail")

        if os.path.exists(fail_dir):
            if not force:
                raise Exception("Build failed already, will not build again")
        if os.path.exists(success_dir):
            if not force:
                return

        if os.path.exists(build_root):
            shutil.rmtree(build_root)

        os.mkdir(build_root)
        os.mkdir(lock_dir)

        if not os.path.exists(build_root):
            raise Exception("Failed to make build_root %r" % build_root)

        print(build_root) 
        p = subprocess.Popen([os.path.join(TUNGSTEN_CLONE_DIR, "setup_builds.sh")], shell=True, cwd=build_root)
        if p.wait() != 0:
            os.mkdir(fail_dir)
            os.rmdir(lock_dir)
            self.builds[commit] = {"success": False}
            raise Exception("Failed to setup_builds")

        p = subprocess.Popen(["make", "-j", str(multiprocessing.cpu_count())], cwd=os.path.join(build_root, "build", "release"))
        if p.wait() != 0:
            os.mkdir(fail_dir)
            os.rmdir(lock_dir)
            self.builds[commit] = {"success": False}
            raise Exception("Failed to build")

        if not os.path.exists(os.path.join(build_root, "build", "release", "tungsten")):
            os.mkdir(fail_dir)
            os.rmdir(lock_dir)
            self.builds[commit] = {"success": False}
            raise Exception("Failed to build")

        self.builds[commit] = {"exec": os.path.join(build_root, "build", "release", "tungsten"),
                "success": True}

        os.mkdir(success_dir)
        os.rmdir(lock_dir)



