import os
import pygit2

_git_dir = pygit2.discover_repository(os.path.realpath(__file__))

GIT_ROOT = pygit2.Repository(_git_dir).workdir

# A temp directory to store tungsten builds
TUNGSTEN_BUILD_DIR = os.path.join(GIT_ROOT, "tbuild")

# A temp directory into which to keep a tungsten clone"
TUNGSTEN_CLONE_DIR = "/tmp/tungsten_clone"

TUNGSTEN_REPO = "https://github.com/tunabrain/tungsten.git"

