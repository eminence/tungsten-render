from django.conf.urls import patterns, include, url
from django.contrib import admin

urlpatterns = patterns('',
    # Examples:
    url(r'^/?$', "tungsten_render.views.post_render", name="post_render"),
    url(r'^/(?P<uid>[0-9a-f]{32})/scene.json', "tungsten_render.views.get_scene_doc", name="get_scene_doc"),
    url(r'^/(?P<uid>[0-9a-f]{32})/render.png', "tungsten_render.views.get_render", name="get_render"),

)
