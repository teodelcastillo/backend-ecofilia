from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0004_alter_user_managers"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="organization",
            field=models.CharField(
                blank=True,
                help_text="Client organization slug (e.g. 'caf'). Null = internal user.",
                max_length=50,
                null=True,
            ),
        ),
    ]
