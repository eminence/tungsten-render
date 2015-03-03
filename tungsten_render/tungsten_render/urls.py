from django.conf.urls import patterns, include, url
from django.contrib import admin

urlpatterns = patterns('',
    # Examples:
    url(r'^/?$', "tungsten_render.views.post_render", name="render"),
    url(r'^/bootstrap.sh?$', "tungsten_render.views.bootstrap", name="bootstrap"),
    url(r'^/startup.sh?$', "tungsten_render.views.startup", name="startup"),
    url(r'^s$', "tungsten_render.views.recent_renders", name="recent_renders"),
    url(r'^/(?P<uid>[0-9a-f]{32})/scene.json', "tungsten_render.views.get_scene_doc", name="get_scene_doc"),
    url(r'^/(?P<uid>[0-9a-f]{32})/render.png', "tungsten_render.views.get_render", name="get_render"),
    url(r'^/(?P<uid>[0-9a-f]{32})/preview.png', "tungsten_render.views.get_preview", name="get_preview"),
    url(r'^/(?P<uid>[0-9a-f]{32})/thumb.png', "tungsten_render.views.get_thumb", name="get_thumb"),
    url(r'^/(?P<uid>[0-9a-f]{32})/status', "tungsten_render.views.get_render_status", name="get_render_status"),

)
