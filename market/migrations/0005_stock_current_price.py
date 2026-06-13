from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("market", "0004_stock_is_active"),
    ]

    operations = [
        migrations.AddField(
            model_name="stock",
            name="current_price",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Latest live price from KIS; refreshed for held stocks during trading hours.",
                null=True,
                verbose_name="Current price",
            ),
        ),
        migrations.AddField(
            model_name="stock",
            name="price_updated_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Price updated at"
            ),
        ),
    ]
