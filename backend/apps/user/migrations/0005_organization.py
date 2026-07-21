from django.db import migrations, models
import django.db.models.deletion


def migrate_org_slug_to_fk(apps, schema_editor):
    """
    0005_user_organization (merged separately, same day) added `organization`
    as a plain CharField slug and was already applied in production before
    this migration existed. Convert any values it wrote into real
    Organization rows + FK links instead of discarding them.
    """
    User = apps.get_model("user", "User")
    Organization = apps.get_model("user", "Organization")
    db_alias = schema_editor.connection.alias

    slugs = (
        User.objects.using(db_alias)
        .exclude(organization_old__isnull=True)
        .exclude(organization_old="")
        .values_list("organization_old", flat=True)
        .distinct()
    )
    for slug in slugs:
        org, _ = Organization.objects.using(db_alias).get_or_create(
            slug=slug,
            defaults={"name": slug.upper()},
        )
        User.objects.using(db_alias).filter(organization_old=slug).update(
            organization=org
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0001_initial"),
        ("user", "0004_alter_user_managers"),
        # 0005_user_organization was merged independently the same day and
        # already applied in production, creating a second 0005 leaf node.
        # Depending on it here resolves the conflicting-leaves error and lets
        # us migrate its data below instead of silently dropping it.
        ("user", "0005_user_organization"),
    ]

    operations = [
        migrations.RenameField(
            model_name="user",
            old_name="organization",
            new_name="organization_old",
        ),
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
        migrations.RunPython(migrate_org_slug_to_fk, noop_reverse),
        migrations.RemoveField(
            model_name="user",
            name="organization_old",
        ),
    ]
