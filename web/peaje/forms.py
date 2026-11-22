from decimal import Decimal

from django import forms

from .models import Vehiculo
from .placas import es_formato_bolivia, normalizar_placa


class VehiculoForm(forms.ModelForm):
    """Registro simple: placa y saldo inicial. Categoría/ejes se guardan como auto / 2 y el propietario vacío."""

    class Meta:
        model = Vehiculo
        fields = ["placa", "saldo"]
        labels = {"placa": "Placa", "saldo": "Saldo inicial (Bs)"}
        help_texts = {
            "placa": "3 o 4 números y 3 letras. Se aceptan guiones y espacios.",
            "saldo": "Monto con el que empieza la cuenta. Puede ser 0.",
        }
        widgets = {
            "placa": forms.TextInput(attrs={"placeholder": "2451KTP", "maxlength": 12, "autocomplete": "off",
                                            "autocapitalize": "characters"}),
            "saldo": forms.NumberInput(attrs={"min": "0", "step": "0.01", "inputmode": "decimal"}),
        }
        error_messages = {"placa": {"required": "Escribe la placa."}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["saldo"].required = False

    def clean_placa(self):
        placa = normalizar_placa(self.cleaned_data["placa"])
        if not es_formato_bolivia(placa):
            raise forms.ValidationError("Formato no válido. Debe ser 3 o 4 números y 3 letras, por ejemplo 2451KTP.")
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
