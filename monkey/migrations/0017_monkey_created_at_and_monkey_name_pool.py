import django.utils.timezone
from django.db import migrations, models

NAMES = [
    "Arthur",
    "Bella",
    "Bingo",
    "Buddy",
    "Charlie",
    "Coco",
    "Cooper",
    "Daisy",
    "Duke",
    "Ellie",
    "Felix",
    "Ginger",
    "Gizmo",
    "Harley",
    "Hazel",
    "Jack",
    "Jasper",
    "Kiwi",
    "Leo",
    "Lily",
    "Lucky",
    "Lucy",
    "Luna",
    "Max",
    "Maggie",
    "Milo",
    "Mochi",
    "Molly",
    "Nala",
    "Nemo",
    "Oliver",
    "Oreo",
    "Oscar",
    "Penny",
    "Pepper",
    "Rex",
    "Rocky",
    "Romeo",
    "Rosie",
    "Ruby",
    "Sadie",
    "Sam",
    "Shadow",
    "Simba",
    "Sophie",
    "Stella",
    "Teddy",
    "Tiger",
    "Toby",
    "Zoe",
    "Zeus",
]


def seed_monkey_names(apps, schema_editor):
    MonkeyName = apps.get_model("monkey", "MonkeyName")
    MonkeyName.objects.bulk_create(
        [MonkeyName(name=name) for name in NAMES],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0016_schedule_price_and_execution_tasks"),
    ]

    operations = [
        migrations.AddField(
            model_name="monkey",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True,
                default=django.utils.timezone.now,
                verbose_name="Created at",
            ),
            preserve_default=False,
        ),
        migrations.CreateModel(
            name="MonkeyName",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(max_length=32, unique=True, verbose_name="Name"),
                ),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.RunPython(seed_monkey_names, migrations.RunPython.noop),
    ]
