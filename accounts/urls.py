from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views
from .views import PasswordChangeWithMessageView, profile, signup

urlpatterns = [
    path("signup/", signup, name="signup"),
    path("profile/", profile, name="profile"),
    path(
        "password-change/",
        PasswordChangeWithMessageView.as_view(
            template_name="accounts/password_change.html",
            success_url=reverse_lazy("profile"),
        ),
        name="password_change",
    ),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
]
