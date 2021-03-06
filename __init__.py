import logging
import asyncio
import random
from datetime import datetime

import pytz
import boto3
import botocore
from prettytable import PrettyTable, MSWORD_FRIENDLY

from opsdroid.matchers import match_apiai_action, match_crontab, match_regex
from opsdroid.message import Message


_LOGGER = logging.getLogger(__name__)


################################################################################
# Helper functions                                                             #
################################################################################


def clean_tags(array_of_tags):
    """Convert a list of boto tags into a dictionary."""
    dict_of_tags = {}
    for tag in array_of_tags:
        dict_of_tags[tag["Key"]] = tag["Value"]
    return dict_of_tags


async def get_office_hours_instances(client, only_stopped=False):
    """List instances that are office hours only, assumes true of not specified."""
    response = client.describe_instances()
    instances = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            tags = clean_tags(instance["Tags"]) if "Tags" in instance else {}
            if "aws:autoscaling:groupName" not in tags\
                    and ("InstanceLifecycle" not in instance or instance["InstanceLifecycle"] != "spot"):
                if "OfficeHours" not in tags or tags["OfficeHours"].lower() != 'false':
                    if only_stopped is False or "StoppedByOfficeHours" in tags:
                        instances.append(instance["InstanceId"])
    return instances


async def aws_get_client(service, config):
    """Get a boto ec2 client."""
    if "aws_access_key_id" in config \
            and "aws_secret_access_key" in config \
            and "region_name" in config:
        client = boto3.client(service,
                              aws_access_key_id=config["aws_access_key_id"],
                              aws_secret_access_key=config["aws_secret_access_key"],
                              region_name=config["region_name"])
    elif "region_name" in config:
        client = boto3.client(service, region_name=config["region_name"])
    else:
        client = boto3.client(service)
    return client


async def aws_watch_instance_state_until_change(client, instanceid, state, message):
    """Check for an instance state change."""
    new_state = state
    check_count = 0
    while state == new_state or check_count > 60:
        check_count = check_count + 1
        response = client.describe_instance_status(InstanceIds=[instanceid])
        _LOGGER.debug("Waiting for %s to change state", instanceid)
        if len(response["InstanceStatuses"]) > 0:
            new_state = response["InstanceStatuses"][0]["InstanceState"]["Name"]
            if new_state != state:
                await message.respond("Instance {} is now {}".format(instanceid, new_state))
        await asyncio.sleep(5)


################################################################################
# Skills                                                                       #
################################################################################


@match_apiai_action("aws.ec2.list")
async def aws_list_servers(opsdroid, config, message):
    """Skill to list instances."""
    client = await aws_get_client('ec2', config)
    response = client.describe_instances(
        Filters=[{'Name': 'instance-state-name','Values': ['running']}])
    table = PrettyTable()
    table.field_names = ["Name", "ID", "State", "IP", "Uptime"]
    table.align = 'l'
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            _LOGGER.debug(instance)
            name = ""
            if 'Tags' in instance:
                names = [x["Value"] for x in instance["Tags"] if x["Key"] == "Name"]
                if len(names) == 1:
                    name = names[0]
            ip = ""
            uptime = datetime.utcnow().replace(tzinfo=pytz.utc) - instance["LaunchTime"]
            if uptime.days < 1:
                uptime = "<1d"
            else:
                uptime = str(uptime.days) + "d"
            if "PublicIpAddress" in instance:
                ip = instance["PublicIpAddress"]
            table.add_row([name, instance["InstanceId"],
                           instance["State"]["Name"], ip, uptime])
    await message.respond("```\n{}\n```".format(table.get_string()))


@match_apiai_action("aws.ec2.count")
async def aws_count_servers(opsdroid, config, message):
    """Skill to count instances in a certain state."""
    status = message.apiai["result"]["parameters"]["server-status"]
    client = await aws_get_client('ec2', config)
    response = client.describe_instances(
        Filters=[{'Name': 'instance-state-name','Values': [status]}])
    await message.respond(
        "There are {} servers {}".format(len(response["Reservations"]), status))


@match_apiai_action("aws.ec2.start")
async def aws_start_server(opsdroid, config, message):
    """Skill to start an instance."""
    instanceid = message.apiai["result"]["parameters"]["server"]
    client = await aws_get_client('ec2', config)
    response = client.start_instances(InstanceIds=[instanceid])
    try:
        response = client.start_instances(InstanceIds=[instanceid])
        for instance in response["StartingInstances"]:
            await message.respond(
                "Changed instance {} to {}".format(instance["InstanceId"],
                                                   instance["CurrentState"]["Name"]))
            await aws_watch_instance_state_until_change(client, instance["InstanceId"],
                                                        instance["CurrentState"]["Name"],
                                                        message)
    except botocore.exceptions.ClientError as e:
         await message.respond("{}".format(e))


@match_apiai_action("aws.ec2.stop")
async def aws_stop_server(opsdroid, config, message):
    """Skill to stop an instance."""
    instanceid = message.apiai["result"]["parameters"]["server"]
    client = await aws_get_client('ec2', config)
    try:
        response = client.stop_instances(InstanceIds=[instanceid])
        for instance in response["StoppingInstances"]:
            await message.respond(
                "Changed instance {} to {}".format(instance["InstanceId"],
                                                   instance["CurrentState"]["Name"]))
    except botocore.exceptions.ClientError as e:
         await message.respond("{}".format(e))


@match_crontab("30 17 * * 1-5")
@match_apiai_action("aws.ec2.devstop")
async def aws_stop_dev(opsdroid, config, message):
    """Skill to stop office hours only instances at the end of the day."""
    if message is None:
        connector = opsdroid.default_connector
        message = Message("", None, connector.default_room, connector)
    byes = [
       "Right, I'm done for the day. Shutting down and going home, Bye!",
       "Ok I'm off. Shutting down the dev stacks. See ya!"
    ]
    await message.respond(random.choice(byes))
    client = await aws_get_client('ec2', config)
    instances = await get_office_hours_instances(client)
    if len(instances) > 0:
        await message.respond("Shutting down {} dev instances".format(len(instances)))
        try:
            client.stop_instances(InstanceIds=instances)
            client.create_tags(Resources=instances, Tags=[{'Key': 'StoppedByOfficeHours', 'Value': 'true'}])
        except botocore.exceptions.ClientError as e:
             await message.respond("{}".format(e))
    else:
        await message.respond("I couldn't find any instances to shut down")


@match_crontab("00 08 * * 1-5")
@match_apiai_action("aws.ec2.devstart")
async def aws_stop_dev(opsdroid, config, message):
    """Skill to start office hours only instances at the beginning of the day."""
    if message is None:
        connector = opsdroid.default_connector
        message = Message("", None, connector.default_room, connector)
    hellos = [
        "Morning everyone, I'm in work bright and early and ready to get stuff done!",
        "Morning all, let's have a productive day!"
    ]
    await message.respond(random.choice(hellos))
    client = await aws_get_client('ec2', config)
    instances = await get_office_hours_instances(client, only_stopped=True)
    if len(instances) > 0:
        await message.respond("Starting {} dev instances".format(len(instances)))
        try:
            client.start_instances(InstanceIds=instances)
            client.delete_tags(Resources=instances, Tags=[{'Key': 'StoppedByOfficeHours'}])
        except botocore.exceptions.ClientError as e:
             await message.respond("{}".format(e))
    else:
        await message.respond("I couldn't find any instances to start")
