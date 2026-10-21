from django.shortcuts import render
from .models import Competition


def competition_list(request):
    competitions = Competition.objects.all()

    return render(
        request,
        "competitions/list.html",
        {"competitions": competitions}
        
    )
# Create your views here.
