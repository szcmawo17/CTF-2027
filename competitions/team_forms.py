from django import forms

from .models import Team


class TeamCreationForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ("name",)
        labels = {"name": "Название команды"}


class TeamInvitationForm(forms.Form):
    username = forms.CharField(label="Имя пользователя", max_length=150)
