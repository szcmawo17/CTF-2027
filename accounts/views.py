from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .forms import ProfileForm


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("profile")

    form = UserCreationForm(request.POST if request.method == "POST" else None)

    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("profile")

    return render(request, "accounts/signup.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    form = ProfileForm(
        request.POST if request.method == "POST" else None,
        instance=request.user,
    )

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Контактные данные сохранены.")
        return redirect("profile")

    return render(request, "accounts/profile.html", {
        "form": form,
        "team_memberships": request.user.team_memberships.select_related("team"),
        "pending_invitations": request.user.team_invitations.filter(
            status="pending"
        ).select_related("team"),
    })


class PasswordChangeWithMessageView(SuccessMessageMixin, PasswordChangeView):
    success_message = "Пароль изменён."
