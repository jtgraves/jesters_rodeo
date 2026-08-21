#!/usr/bin/env python3
import aws_cdk as cdk

from jesters_rodeo_stack import JestersRodeoStack

app = cdk.App()
JestersRodeoStack(app, "JestersRodeoStack")
app.synth()
