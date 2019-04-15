# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2019-04-01 05:38
from __future__ import unicode_literals

import commercialoperator.components.compliances.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commercialoperator', '0062_auto_20190401_1103'),
    ]

    operations = [
        migrations.AddField(
            model_name='proposalotherdetails',
            name='credit_docket_books',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='proposalotherdetails',
            name='credit_fees',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='compliancedocument',
            name='_file',
            field=models.FileField(upload_to=commercialoperator.components.compliances.models.update_proposal_complaince_filename),
        ),
    ]
