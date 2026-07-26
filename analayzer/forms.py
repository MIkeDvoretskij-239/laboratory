from django import forms
from django.forms import inlineformset_factory

from .models import CalibrationBatch, InstrumentProfile, RawUpload, SampleBatch, SampleUpload


class CalibrationBatchForm(forms.ModelForm):
    class Meta:
        model = CalibrationBatch
        fields = ["name", "notes"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Например: Калибровка 2026-07-24"}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        }


class RawUploadForm(forms.ModelForm):
    class Meta:
        model = RawUpload
        fields = ["label", "file", "profile", "name_filter"]
        widgets = {
            "label": forms.TextInput(attrs={"class": "form-control", "placeholder": "H2O / Plazma / Mike ..."}),
            "file": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "profile": forms.Select(attrs={"class": "form-select"}),
            "name_filter": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "необязательно, напр. (M)"}
            ),
        }


RawUploadFormSet = inlineformset_factory(
    CalibrationBatch,
    RawUpload,
    form=RawUploadForm,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class SampleBatchForm(forms.ModelForm):
    class Meta:
        model = SampleBatch
        fields = ["name", "notes"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Например: Пробы 2026-07-25"}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        }


class SampleUploadForm(forms.ModelForm):
    class Meta:
        model = SampleUpload
        fields = ["label", "file", "profile", "name_filter"]
        widgets = {
            "label": forms.TextInput(attrs={"class": "form-control", "placeholder": "необязательно"}),
            "file": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "profile": forms.Select(attrs={"class": "form-select"}),
            "name_filter": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "необязательно, напр. (M)"}
            ),
        }


SampleUploadFormSet = inlineformset_factory(
    SampleBatch,
    SampleUpload,
    form=SampleUploadForm,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class InstrumentProfileForm(forms.ModelForm):
    column_names_text = forms.CharField(
        label="Названия колонок (по одной на строку, в порядке слева направо)",
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 12, "placeholder": "№\n#\nName\nType\nStd.Conc\n..."}),
    )

    class Meta:
        model = InstrumentProfile
        fields = ["name", "notes"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Например: 18 columns (full export)"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.column_names:
            self.fields["column_names_text"].initial = "\n".join(self.instance.column_names)
        # column_names_text isn't a model field, so it must come after the model fields
        # in field order for the template to render name/notes/columns in a sane sequence.
        self.order_fields(["name", "column_names_text", "notes"])

    def clean_column_names_text(self):
        raw = self.cleaned_data["column_names_text"]
        names = [line.strip() for line in raw.splitlines() if line.strip()]
        if len(names) < 2:
            raise forms.ValidationError("Укажи хотя бы 2 названия колонок — по одному на строку.")
        return names

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.column_names = self.cleaned_data["column_names_text"]
        if commit:
            instance.save()
        return instance
