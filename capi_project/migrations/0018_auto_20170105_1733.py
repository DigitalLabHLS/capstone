# -*- coding: utf-8 -*-
# Generated by Django 1.10.2 on 2017-01-05 17:33
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('capi_project', '0017_auto_20170105_1713'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='reporter',
            name='jurisdiction',
        ),
        migrations.RenameField('reporter', 'jurisdiction_name', 'jurisdiction'),
    ]
