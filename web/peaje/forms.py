from decimal import Decimal

from django import forms

from .models import Vehiculo
from .placas import es_formato_bolivia, normalizar_placa


class VehiculoForm(forms.ModelForm):
    """Registro simple. Categoría y ejes no se muestran por ahora: se guardan como auto / 2 ejes."""

    class Meta:
        model = Vehiculo
        fields = ["placa", "propietario", "saldo"]
        labels = {"placa": "Placa", "propietario": "Propietario", "saldo": "Saldo inicial (Bs)"}
        widgets = {
            "placa": forms.TextInput(attrs={"placeholder": "Ej. 2451KTP", "maxlength": 12, "autocomplete": "off"}),
            "propietario": forms.TextInput(attrs={"placeholder": "Nombre o empresa"}),
            "saldo": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
        }

    def clean_placa(self):
        placa = normalizar_placa(self.cleaned_data["placa"])
        if not es_formato_bolivia(placa):
            raise forms.ValidationError("Formato boliviano: 3 o 4 números y 3 letras (ej. 2451KTP).")
        if Vehiculo.objects.filter(placa=placa).exists():
            raise forms.ValidationError(f"La placa {placa} ya está registrada.")
        return placa

    def clean_saldo(self):
        saldo = self.cleaned_data.get("saldo") or Decimal("0")
        if saldo < 0:
            raise forms.ValidationError("El saldo no puede ser negativo.")
        return saldo

    def save(self, commit=True):
        vehiculo = super().save(commit=False)
        vehiculo.categoria, vehiculo.ejes = "auto", 2
        if commit:
            vehiculo.save()
        return vehiculo
