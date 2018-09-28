# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-08-27 06:22
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mooring', '0038_auto_20180827_1341'),
    ]

    operations = [
        migrations.AlterField(
            model_name='rate',
            name='adult',
            field=models.DecimalField(blank=True, decimal_places=2, default='10.00', max_digits=8, null=True),
        ),
        migrations.AlterField(
            model_name='rate',
            name='child',
            field=models.DecimalField(blank=True, decimal_places=2, default='2.20', max_digits=8, null=True),
        ),
        migrations.AlterField(
            model_name='rate',
            name='concession',
            field=models.DecimalField(blank=True, decimal_places=2, default='6.60', max_digits=8, null=True),
        ),
        migrations.AlterField(
            model_name='rate',
            name='infant',
            field=models.DecimalField(blank=True, decimal_places=2, default='0', max_digits=8, null=True),
        ),
        migrations.AlterUniqueTogether(
            name='rate',
            unique_together=set([]),
        ),
    ]
