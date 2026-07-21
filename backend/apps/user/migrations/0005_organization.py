from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0001_initial"),
        ("user", "0004_alter_user_managers"),
    ]

    operations = [
        migrations.CreateModel(
            name="Organization",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("slug", models.SlugField(max_length=64, unique=True)),
                (
                    "restricted",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "When true, members only see the sections in enabled_features and "
                            "the skills in enabled_skills; they cannot create their own skills "
                            "or evaluations."
                        ),
                    ),
                ),
                (
                    "enabled_features",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text=(
                            'Sections visible to members of a restricted org, e.g. '
                            '["projects", "library", "chat"]. Ignored when restricted=False.'
                        ),
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "enabled_skills",
                    models.ManyToManyField(
                        blank=True,
                        help_text=(
                            "Skills (including workflow agents) enabled for members of a "
                            "restricted org. Ignored when restricted=False."
                        ),
                        related_name="enabled_for_organizations",
                        to="skill.skill",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="user",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                help_text="Client organization this user belongs to (e.g. CAF).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="members",
                to="user.organization",
            ),
        ),
    ]
