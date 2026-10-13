from django.db import models


class FuelStation(models.Model):
    opis_id = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=6, decimal_places=3)  # USD / gallon
    lat = models.FloatField()
    lng = models.FloatField()

    class Meta:
        indexes = [models.Index(fields=["lat", "lng"])]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
