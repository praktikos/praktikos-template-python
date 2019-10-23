#!/usr/bin/env python3

import json
import logging
import sys
import time
from os import path

import boto3

logger = logging.getLogger(__name__)

root_directory = path.abspath(path.dirname(__file__))
configs_directory = root_directory + path.sep + "configs"


def migrate_database():
    from api.rdb.model.schema import db_migrate
    """Create and ensure proper schema"""
    logger.info("migrating database")
    db_migrate()


# noinspection PyMethodMayBeStatic
def get_file_contents(filename, mode="r"):
    with open(filename, mode) as f:
        contents = f.read()
        f.close()
        return contents


def create_iam_role(iam_client, lambda_role_name, description, json_policy):
    return iam_client.create_role(
        RoleName=lambda_role_name,
        AssumeRolePolicyDocument=json_policy,
        Description=description
    )


def create_iam_user_and_group_policies(iam_client, user_name, group_name, policy_arns):
    # type: ('boto3.client("iam")', str, str, list) -> dict

    try:
        iam_client.create_user(UserName=user_name)
        logger.info("User created:" + user_name)
    except Exception as ex:
        # it's ok if user already exists
        # noinspection PyUnresolvedReferences
        if ex.response['Error']['Code'] == "EntityAlreadyExists":
            pass
    try:
        iam_client.create_group(GroupName=group_name)
        logger.info("Group created:" + group_name)
    except Exception as ex:
        # it's ok if group already exists
        # noinspection PyUnresolvedReferences
        if ex.response['Error']['Code'] == "EntityAlreadyExists":
            pass

    for policy_arn in policy_arns:
        # it's ok if group policy is already attached to group
        iam_client.attach_group_policy(GroupName=group_name, PolicyArn=policy_arn)
        logger.info("Policy attached to group:" + policy_arn)

    # it's ok if user is already added to group
    iam_client.add_user_to_group(GroupName=group_name, UserName=user_name)
    logger.info("User %s attached to group %s." % (user_name, group_name))
    response = iam_client.create_access_key(UserName=user_name)
    time.sleep(10)
    return response


def create_cognito_user_pool(cognito_idp_client, user_pool_config):
    # type: ('boto3.client("cognito-idp")', dict) -> str
    # noinspection PyBroadException,PyUnusedLocal
    try:
        kwargs = {
            "MaxResults": 60
        }
        while True:
            response = cognito_idp_client.list_user_pools(**kwargs)
            # yield from response['events']
            try:
                kwargs['NextToken'] = response['NextToken']
            except KeyError:
                break
        for user_pool in response['UserPools']:
            if user_pool['Name'] == user_pool_config['PoolName']:
                return user_pool['Id']

        response = cognito_idp_client.create_user_pool(**user_pool_config)
        return response['UserPool']['Id']
    except Exception as ex:
        pass

    # try:
    #     response = cognito_idp_client.create_user_pool_domain(
    #         Domain='praktikos',
    #         UserPoolId='string'
    #     )
    # except Exception as ex:
    #     pass


def fix_identity_pool_id(trust_policy_str, identity_pool_id):
    # type: (str, str) -> str
    trust_policy_dict = json.loads(trust_policy_str)
    trust_policy_dict['Statement'][0]['Condition']['StringEquals'][
        'cognito-identity.amazonaws.com:aud'] = identity_pool_id
    return json.dumps(trust_policy_dict)


# noinspection PyUnusedLocal
def create_cognito_identity_pool(cognito_identity_client,
                                 iam_client,
                                 identity_pool_name,
                                 allow_unauthenticated_identities,
                                 cognito_praktikos_trust_policy_auth_file,
                                 cognito_praktikos_trust_policy_unauth_file
                                 ):
    # type: ('boto3.client("cognito-identity")', 'boto3.client("iam")', str, bool, str, str) -> None
    identity_pools = cognito_identity_client.list_identity_pools(MaxResults=60)
    identity_pool_id = None
    for identity_pool in identity_pools['IdentityPools']:
        if identity_pool['IdentityPoolName'] == identity_pool_name:
            identity_pool_id = identity_pool['IdentityPoolId']
    if not identity_pool_id:
        cognito_identity_client.create_identity_pool(
            IdentityPoolName=identity_pool_name,
            AllowUnauthenticatedIdentities=allow_unauthenticated_identities
        )
        # recurse only once
        create_cognito_identity_pool(cognito_identity_client,
                                     iam_client,
                                     identity_pool_name,
                                     allow_unauthenticated_identities,
                                     cognito_praktikos_trust_policy_auth_file,
                                     cognito_praktikos_trust_policy_unauth_file
                                     )
    response = cognito_identity_client.get_identity_pool_roles(IdentityPoolId=identity_pool_id)
    if 'Roles' not in response:
        # noinspection PyBroadException
        try:
            auth_role = iam_client.get_role(RoleName="Cognito_praktikosAuth_Role")
        except Exception as ex:
            json_policy = get_file_contents(cognito_praktikos_trust_policy_auth_file)
            auth_role = create_iam_role(iam_client,
                                        "Cognito_praktikosAuth_Role",
                                        "Cognito Authenticated role for Praktikos Lambda Functions",
                                        fix_identity_pool_id(json_policy, identity_pool_id)
                                        )
        # noinspection PyBroadException
        try:
            unauth_role = iam_client.get_role(RoleName="Cognito_praktikosUnauth_Role")
        except Exception as ex:
            json_policy = get_file_contents(cognito_praktikos_trust_policy_unauth_file)
            unauth_role = create_iam_role(iam_client,
                                          "Cognito_praktikosUnauth_Role",
                                          "Cognito Unauthenticated role for Praktikos Lambda Functions",
                                          fix_identity_pool_id(json_policy, identity_pool_id)
                                          )

        kwargs = {
            "IdentityPoolId": identity_pool_id,
            "Roles": {
                "authenticated": auth_role['Role']['Arn'],
                "unauthenticated": unauth_role['Role']['Arn']
            }
        }
        cognito_identity_client.set_identity_pool_roles(**kwargs)


def create_cognito_user_pool_client(cognito_idp_client, user_pool_id, client_name, cognito_user_pool_client_file):
    # type: ('boto3.client("cognito-idp")', str, str, str) -> None
    user_pool_clients = cognito_idp_client.list_user_pool_clients(UserPoolId=user_pool_id, MaxResults=60)
    for user_pool_client in user_pool_clients['UserPoolClients']:
        if user_pool_client['ClientName'] == client_name:
            return
    # user pool client not found, create it
    kwargs = json.loads(get_file_contents(cognito_user_pool_client_file))
    kwargs['UserPoolId'] = user_pool_id
    kwargs['ClientName'] = client_name
    cognito_idp_client.create_user_pool_client(**kwargs)


def create_cognito_user_pool_group(cognito_idp_client, user_pool_id, group_name):
    kwargs = {
        "UserPoolId": user_pool_id,
        "Limit": 60
    }
    while True:
        response = cognito_idp_client.list_groups(**kwargs)
        for group in response['Groups']:
            if group['GroupName'] == group_name:
                return
        # yield from response['events']
        try:
            kwargs['NextToken'] = response['NextToken']
        except KeyError:
            break
    # group not found, create it
    cognito_idp_client.create_group(
        GroupName=group_name,
        UserPoolId=user_pool_id
    )


def create_iam_user_praktikos(iam_client, user_name="praktikos", group_name="praktikos"):
    # type: ('boto3.client("iam")', str, str) -> dict
    filename = path.abspath(path.join(configs_directory, "iam_user_and_group_policy_arns.json"))
    with open(filename, 'r') as fd:
        policy_arns = json.loads(fd.read())
    return create_iam_user_and_group_policies(iam_client, user_name, group_name, policy_arns)


def create_cognito_user_pool_praktikos(cognito_idp_client):
    filename = path.abspath(path.join(configs_directory, "cognito_user_pool.json"))
    with open(filename, 'r') as fd:
        cognito_user_pool = json.loads(fd.read())
    user_pool_id = create_cognito_user_pool(cognito_idp_client, cognito_user_pool)
    filename = path.abspath(path.join(configs_directory, "cognito_user_pool_client.json"))
    create_cognito_user_pool_client(cognito_idp_client, user_pool_id, "praktikos", filename)
    create_cognito_user_pool_group(cognito_idp_client, user_pool_id, "authenticated")


def create_cognito_identity_pool_praktikos(cognito_identity_client, iam_client):
    auth_filename = path.abspath(path.join(configs_directory, "trust_policy_cognito_auth.json"))
    unauth_filename = path.abspath(path.join(configs_directory, "trust_policy_cognito_unauth.json"))
    create_cognito_identity_pool(cognito_identity_client,
                                 iam_client,
                                 "praktikos",
                                 True,
                                 auth_filename,
                                 unauth_filename
                                 )


def config():
    # example: ./cli.py config LambdaApiTemplateJS region
    if len(sys.argv) > 2:
        lambda_directory = path.abspath(path.join(path.dirname(__file__), "lambda_functions", sys.argv[2]))
        filename = "%s/config.json" % lambda_directory
        with open(filename, 'r') as fd:
            contents = json.load(fd)
            contents.update({'account_id': boto3.client("sts").get_caller_identity()["Account"]})
            val = contents[sys.argv[3]]
            # print(val)
            return val


def main():
    """
    administration
    """
    # if len(sys.argv) > 1:
    #     if sys.argv[1] == "migrate":
    #         migrate_database()
    #     elif sys.argv[1] == "config":
    #         sys.exit(config())
    #     else:
    #         raise Exception("Unrecognized command: %s" % sys.argv[1])

    # ##################################################################################################################
    # 1. create an IAM user with all of the roles necessary to bootstrap AWS services required by Praktikos
    # The user in ~/.aws/credentials must have a IAM FullAccess policy attached to user
    # https://console.aws.amazon.com/iam/home?region=us-east-1#/policies/arn:aws:iam::aws:policy/IAMFullAccess$jsonEditor
    iam_client = boto3.client('iam')
    response = create_iam_user_praktikos(iam_client, "praktikos", "praktikos")
    # save off temporary  access key
    user_name = response['AccessKey']['UserName']
    aws_access_key_id = response['AccessKey']['AccessKeyId']
    aws_secret_access_key = response['AccessKey']['SecretAccessKey']
    iam_client = boto3.client('iam',
                              aws_access_key_id=aws_access_key_id,
                              aws_secret_access_key=aws_secret_access_key)
    cognito_idp_client = boto3.client('cognito-idp',
                                      aws_access_key_id=aws_access_key_id,
                                      aws_secret_access_key=aws_secret_access_key)
    cognito_identity_client = boto3.client('cognito-identity',
                                           aws_access_key_id=aws_access_key_id,
                                           aws_secret_access_key=aws_secret_access_key)

    # sts_client = boto3.client("sts")
    # x = sts_client.get_caller_identity()
    # aws_account_id = x["Account"]
    # aws_account_id = iam_client.CurrentUser().arn.split(':')[4]
    # ##################################################################################################################

    try:
        # ##############################################################################################################
        # 2. create a AWS Cognito User Pool
        create_cognito_user_pool_praktikos(cognito_idp_client)
        # ##############################################################################################################

        # ##############################################################################################################
        # 3. create a AWS Cognito Identity Pool and IAM Lambda Roles
        create_cognito_identity_pool_praktikos(cognito_identity_client, iam_client)
        # ##############################################################################################################

    finally:
        # ##############################################################################################################
        if iam_client:
            # delete temporary access key
            iam_client.delete_access_key(
                UserName=user_name,
                AccessKeyId=aws_access_key_id
            )
        # ##############################################################################################################


if __name__ == '__main__':
    """
    administration
    """
    if len(sys.argv) > 1:
        if sys.argv[1] == "migrate":
            migrate_database()
        elif sys.argv[1] == "config":
            sys.exit(config())
        else:
            raise Exception("Unrecognized command: %s" % sys.argv[1])
