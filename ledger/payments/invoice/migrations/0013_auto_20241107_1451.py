# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2024-11-07 06:51
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoice', '0012_auto_20241107_1451'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoice',
            name='invoice_name',
            field=models.CharField(blank=True, default='', max_length=255, null=True),
        ),
    ]
