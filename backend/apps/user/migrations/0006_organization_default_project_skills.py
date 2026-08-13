from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0018_seed_caf_iet_datbc_workflow"),
        ("user", "0005_organization"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="default_project_skills",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Skills every NEW project of this org starts with. Distinct "
                    "from enabled_skills, which only controls visibility: a "
                    "restricted org may see several agents but have only one "
                    "attached by default. Members of restricted orgs cannot "
                    "assign skills themselves, so without this their projects "
                    "would be born with an empty workspace."
                ),
                related_name="default_for_organizations",
                to="skill.skill",
            ),
        ),
    ]
