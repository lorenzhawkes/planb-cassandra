#!/usr/bin/env python3

import boto3
import click
import collections
import yaml
from clickclick import Action, info


def setup_security_groups(cluster_name: str, public_ips: dict) -> dict:
    '''
    Allow traffic between regions

    Returns a dict of region -> security group ID
    '''
    for region, ips in public_ips.items():
        with Action('Configuring security group in {}..'.format(region)):
            ec2 = boto3.client('ec2', region)
            resp = ec2.describe_vpcs()
            # TODO: support more than one VPC..
            vpc_id = resp['Vpcs'][0]['VpcId']
            sg_name = cluster_name
            sg = ec2.create_security_group(GroupName=sg_name, VpcId=vpc_id,
                    Description='Allow cassandra nodes to talk via port 7001')

            ec2.create_tags(Resources=[sg['GroupId']],
                            Tags=[{'Key': 'Name', 'Value': sg_name}])
            ip_permissions = []
            for ip in ips:
                ip_permissions.append({'IpProtocol': 'tcp',
                                       'FromPort': -1,
                                       'ToPort': 7001,
                                       'IpRanges': [{'CidrIp': '{}/32'.format(ip)}]})
            ip_permissions.append({'IpProtocol': '-1',
                                   'UserIdGroupPairs': [{'GroupId': sg['GroupId']}]})
            ec2.authorize_security_group_ingress(GroupId=sg['GroupId'],
                                                 IpPermissions=ip_permissions)


def find_taupage_amis(regions: list) -> dict:
    '''
    Find latest Taupage AMI for each region
    '''
    result = {}
    for region in regions:
        with Action('Finding latest Taupage AMI in {}..'.format(region)):
            ec2 = boto3.resource('ec2', region)
            filters = [{'Name': 'name', 'Values': ['*Taupage-AMI-*']},
                    {'Name': 'is-public', 'Values': ['false']},
                    {'Name': 'state', 'Values': ['available']},
                    {'Name': 'root-device-type', 'Values': ['ebs']}]
            images = list(ec2.images.filter(Filters=filters))
            if not images:
                raise Exception('No Taupage AMI found')
            most_recent_image = sorted(images, key=lambda i: i.name)[-1]
            result[region] = most_recent_image
    return result


def generate_taupage_user_data(cluster_name: str, seed_nodes: list):
    '''
    Generate Taupage user data to start a Cassandra node
    http://docs.stups.io/en/latest/components/taupage.html
    '''
    data = {'runtime': 'Docker',
            'source': 'registry.opensource.zalan.do/stups/planb-cassandra:cd1',
            'application_id': cluster_name,
            'application_version': '1.0',
            'ports': {'7001': '7001',
                '9042': '9042'},
            'environment': {
                'CLUSTER_NAME': cluster_name,
                'SEEDS': ','.join(seed_nodes),
                }
            }
    # TODO: add KMS-encrypted keystore/truststore

    serialized = yaml.safe_dump(data)
    user_data = '#taupage-ami-config\n{}'.format(serialized)
    return user_data


@click.command()
@click.option('--cluster-size', default=3, type=int)
@click.option('--instance-type', default='t2.micro')
@click.argument('cluster_name')
@click.argument('regions', nargs=-1)
def cli(cluster_name: str, regions: list, cluster_size: int, instance_type: str):
    if not regions:
        raise click.UsageError('Please specify at least one region')

    # generate keystore/truststore

    # Elastic IPs by region
    # Let's assume the first IP in each region is the seed node
    public_ips = collections.defaultdict(list)

    # reservice Elastic IPs
    for region in regions:
        with Action('Allocating Public IPs for {}..'.format(region)) as act:
            ec2 = boto3.client('ec2', region_name=region)
            for i in range(cluster_size):
                resp = ec2.allocate_address(Domain='vpc')
                public_ips[region].append(resp['PublicIp'])
                act.progress()

    # Now we have all necessary Public IPs
    # take first IP in every region as seed node
    seed_nodes = []
    for region in regions:
        # TODO: support more than one seed node per region for larger clusters
        seed_nodes.append(public_ips[region][0])
    info('Our seed nodes are: {}'.format(', '.join(seed_nodes)))

    # Set up Security Groups
    security_groups = setup_security_groups(cluster_name, public_ips)

    taupage_amis = find_taupage_amis(regions)
    user_data = generate_taupage_user_data(cluster_name, seed_nodes)

    # Launch EC2 instances with correct user data
    # Launch sequence:
    # start seed nodes (e.g. 1 per region if cluster_size == 3)
    for ip, region in zip(seed_nodes, regions):
        with Action('Launching seed node {}..'.format(ip)):
            ec2 = boto3.client('ec2', region_name=region)
            resp = ec2.describe_subnets()
            # subnet IDs sorted by AZ
            subnets = []
            for subnet in sorted(resp['Subnets'], key=lambda subnet: subnet['AvailabilityZone']):
                subnet.append(subnet['SubnetId'])
            # start seed node in first AZ
            ec2.run_instances(ImageId=taupage_amis[region], MinCount=1, MaxCount=1, SecurityGroupIds=[security_groups[region]],
                    UserData=user_data, InstanceType=instance_type,
                    SubnetId=subnets[0])


    # make sure all seed nodes are up
    # add remaining nodes one by one

if __name__ == '__main__':
    cli()
