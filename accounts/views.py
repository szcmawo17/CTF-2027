from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import render, redirect


def signup(request):
    form = UserCreationForm(
        request.POST or None
    )

    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("/competitions/")

    return render(request, "accounts/signup.html", {
        "form": form
    })