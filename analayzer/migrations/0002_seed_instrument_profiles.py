from django.db import migrations

SHORT_PROFILE = ["№", "Name", "Std.Conc", "Area", "IS Area", "Response", "Conc", "%Rec", "%Dev"]

LONG_PROFILE = [
    "№", "#", "Name", "Type", "Std.Conc", "RT", "Area", "IS Area", "Response",
    "Primary", "Flags", "Conc", "%Rec", "%Dev", "Vial", "Acq.Time\t1º", "Area\t1º", "Ratio (Actual)",
]

PROFILES = [
    {
        "name": "9 columns (short export)",
        "column_names": SHORT_PROFILE,
        "notes": "Как H2O_cal_kate.xlsx / H2O_cal_mike.xlsx — 9 колонок без RT/Type/Vial.",
    },
    {
        "name": "18 columns (full export)",
        "column_names": LONG_PROFILE,
        "notes": "Как plazma_cal.xlsx / cal_h2o_new.xlsx — полный экспорт с Type, RT, Vial и т.д.",
    },
]


def seed_profiles(apps, schema_editor):
    InstrumentProfile = apps.get_model("analayzer", "InstrumentProfile")
    for profile in PROFILES:
        InstrumentProfile.objects.update_or_create(
            name=profile["name"],
            defaults={"column_names": profile["column_names"], "notes": profile["notes"]},
        )


def remove_profiles(apps, schema_editor):
    InstrumentProfile = apps.get_model("analayzer", "InstrumentProfile")
    InstrumentProfile.objects.filter(name__in=[p["name"] for p in PROFILES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("analayzer", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_profiles, remove_profiles),
    ]
