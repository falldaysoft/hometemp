from django.http import HttpResponse
from django.shortcuts import render


def health(request):
    # The container healthcheck in deploy/docker-compose.yml hits this.
    return HttpResponse("ok")


def index(request):
    return render(request, "core/index.html")
