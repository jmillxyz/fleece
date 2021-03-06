#!/usr/bin/env python
import argparse
import base64
import json
import os
import subprocess
import sys

import boto3
import ruamel.yaml as yaml
import six
if six.PY2:  # noqa
    from StringIO import StringIO
else:
    from io import StringIO

from fleece.cli.run import run

if six.PY2:
    input = raw_input


class AWSCredentialCache(object):
    def __init__(self, rs_username, rs_api_key, env_config):
        self.rs_username = rs_username
        self.rs_api_key = rs_api_key
        self.environments = run.get_config(env_config)['environments']
        self.rax_token = None
        self.tenant = None
        self.awscreds = {}

    def _get_rax_token(self):
        if self.rax_token is None:
            self.rax_token, self.tenant = run.get_rackspace_token(
                self.rs_username, self.rs_api_key)
        return self.rax_token, self.tenant

    def get_awscreds(self, environment):
        if environment not in self.awscreds:
            account = None
            for env in self.environments:
                if env['name'] == environment:
                    account = env['account']
                    break
            if account is None:
                raise ValueError('Environment "{}" is not known, add it to '
                                 'environments.yml file'.format(environment))
            token, tenant = self._get_rax_token()
            self.awscreds[environment] = run.get_aws_creds(
                account, tenant, token)
        return self.awscreds[environment]


STATE = {
    'awscreds': None,  # cache of aws credentials
    'stages': {}       # environment/key assignments for each stage
}


def _get_stage_data(stage, data=None):
    if not data:
        data = STATE['stages']
    data = run.get_stage_data(stage, data)
    if data is None:
        raise ValueError('No match for stage "{}"'.format(stage))
    return data


def _get_kms_key(stage):
    stage_data = _get_stage_data(stage)
    try:
        key = stage_data['key']
    except IndexError:
        raise ValueError('No key defined for stage "{}"'.format(stage))
    if key.startswith('alias/') or key.startswith('arn:'):
        return key
    return 'alias/' + key


def _get_environment(stage):
    stage_data = _get_stage_data(stage)
    try:
        return stage_data['environment']
    except IndexError:
        raise ValueError('No environment defined for stage "{}"'.format(stage))


def _encrypt_text(text, stage):
    key = _get_kms_key(stage)
    environment = _get_environment(stage)
    awscreds = STATE['awscreds'].get_awscreds(environment)
    kms = boto3.client('kms', aws_access_key_id=awscreds['accessKeyId'],
                       aws_secret_access_key=awscreds['secretAccessKey'],
                       aws_session_token=awscreds['sessionToken'])
    r = kms.encrypt(KeyId=key, Plaintext=text.encode('utf-8'))
    return base64.b64encode(r['CiphertextBlob']).decode('utf-8')


def _decrypt_text(text, stage):
    environment = _get_environment(stage)
    awscreds = STATE['awscreds'].get_awscreds(environment)
    kms = boto3.client('kms', aws_access_key_id=awscreds['accessKeyId'],
                       aws_secret_access_key=awscreds['secretAccessKey'],
                       aws_session_token=awscreds['sessionToken'])
    r = kms.decrypt(CiphertextBlob=base64.b64decode(text.encode('utf-8')))
    return r['Plaintext'].decode('utf-8')


def _encrypt_item(data, stage, key):
    if (isinstance(data, six.text_type) or isinstance(data, six.binary_type)) \
            and data.startswith(':encrypt:'):
        if not stage:
            sys.stderr.write('Warning: Key "{}" cannot be encrypted because '
                             'it does not belong to a stage\n'.format(key))
        else:
            data = ':decrypt:' + _encrypt_text(data[9:], stage)
    elif isinstance(data, dict):
        per_stage = [k.startswith('+') for k in data]
        if any(per_stage):
            if not all(per_stage):
                raise ValueError('Keys "{}" have a mix of stage and non-stage '
                                 'variables'.format(', '.join(data.keys)))
            key_prefix = key + '.' if key else ''
            for k, v in data.items():
                data[k] = _encrypt_item(v, stage=k[1:], key=key_prefix + k)
        else:
            data = _encrypt_dict(data, stage=stage, key=key)
    elif isinstance(data, list):
        data = _encrypt_list(data, stage=stage, key=key)
    return data


def _encrypt_list(data, stage, key):
    return [_encrypt_item(v, stage=stage, key=key + '[]') for v in data]


def _encrypt_dict(data, stage=None, key=''):
    key_prefix = key + '.' if key else ''
    for k, v in data.items():
        data[k] = _encrypt_item(v, stage=stage, key=key_prefix + k)
    return data


def import_config(args, input_file=None):
    if not input_file:
        input_file = sys.stdin
    source = input_file.read().strip()
    if source[0] == '{':
        # JSON input
        config = json.loads(source)
    else:
        # YAML input
        config = yaml.round_trip_load(source)

    STATE['stages'] = config['stages']
    config['config'] = _encrypt_dict(config['config'])
    with open(args.config, 'wt') as f:
        if config:
            yaml.round_trip_dump(config, f)


def _decrypt_item(data, stage, key, render):
    if (isinstance(data, six.text_type) or isinstance(data, six.binary_type)) \
            and data.startswith(':decrypt:'):
        data = _decrypt_text(data[9:], stage)
        if not render or render == 'ssm':
            data = ':encrypt:' + data
    elif isinstance(data, dict):
        if len(data) == 0:
            return data
        per_stage = [k.startswith('+') for k in data]
        if any(per_stage):
            if not all(per_stage):
                raise ValueError('Keys "{}" have a mix of stage and non-stage '
                                 'variables'.format(', '.join(data.keys)))
        if render:
            if per_stage[0]:
                stage_data = _get_stage_data(
                    stage, data={k[1:]: v for k, v in data.items()})
                if stage_data:
                    data = _decrypt_item(
                        data.get(stage, stage_data),
                        stage=stage, key=key, render=render)
                else:
                    raise ValueError('Key "{}" has no value for stage '
                                     '"{}"'.format(key, stage))
            else:
                data = _decrypt_dict(data, stage=stage, key=key, render=render)
        else:
            key_prefix = key + '.' if key else ''
            for k, v in data.items():
                data[k] = _decrypt_item(v, stage=k[1:], key=key_prefix + k,
                                        render=render)
            data = _decrypt_dict(data, stage=stage, key=key, render=render)
    elif isinstance(data, list):
        data = _decrypt_list(data, stage=stage, key=key, render=render)
    return data


def _decrypt_list(data, stage, key, render):
    return [_decrypt_item(v, stage=stage, key=key + '[]', render=render)
            for v in data]


def _decrypt_dict(data, stage=None, key='', render=False):
    key_prefix = key + '.' if key else ''
    for k, v in data.items():
        data[k] = _decrypt_item(v, stage=stage, key=key_prefix + k,
                                render=render)
    return data


def export_config(args, output_file=None):
    if not output_file:
        output_file = sys.stdout
    if os.path.exists(args.config):
        with open(args.config, 'rt') as f:
            config = yaml.round_trip_load(f.read())
        STATE['stages'] = config['stages']
        config['config'] = _decrypt_dict(config['config'])
    else:
        config = {
            'stages': {
                env['name']: {
                    'environment': env['name'],
                    'key': 'enter-key-name-here'
                } for env in STATE['awscreds'].environments
            },
            'config': {}}
    if args.json:
        output_file.write(json.dumps(config, indent=4))
    elif config:
        yaml.round_trip_dump(config, output_file)


def edit_config(args):
    filename = '.fleece_edit_tmp'
    skip_export = False

    if os.path.exists(filename):
        p = input('A previously interrupted edit session was found. Do you '
                  'want to (C)ontinue that session or (A)bort it? ')
        if p.lower() == 'a':
            os.unlink(filename)
        elif p.lower() == 'c':
            skip_export = True

    if not skip_export:
        with open(filename, 'wt') as fd:
            export_config(args, output_file=fd)

    subprocess.call(args.editor + ' ' + filename, shell=True)

    with open(filename, 'rt') as fd:
        import_config(args, input_file=fd)

    os.unlink(filename)


def render_config(args, output_file=None):
    if not output_file:
        output_file = sys.stdout
    stage = args.stage
    env = args.environment or stage
    with open(args.config, 'rt') as f:
        config = yaml.safe_load(f.read())
    STATE['stages'] = config['stages']
    config['config'] = _decrypt_item(config['config'], stage=stage, key='',
                                     render=True)
    if args.json or args.encrypt or args.python:
        rendered_config = json.dumps(
            config['config'], indent=None if args.encrypt else 4,
            separators=(',', ':') if args.encrypt else (',', ': '))
    else:
        buf = StringIO()
        yaml.round_trip_dump(config['config'], buf)
        rendered_config = buf.getvalue()
    if args.encrypt or args.python:
        STATE['stages'] = config['stages']
        encrypted_config = []
        while rendered_config:
            buffer = _encrypt_text(rendered_config[:4096], env)
            rendered_config = rendered_config[4096:]
            encrypted_config.append(buffer)

        if not args.python:
            rendered_config = json.dumps(encrypted_config)
        else:
            rendered_config = '''ENCRYPTED_CONFIG = {}
import base64
import boto3
import json

def load_config():
    config_json = ''
    kms = boto3.client('kms')
    for buffer in ENCRYPTED_CONFIG:
        r = kms.decrypt(CiphertextBlob=base64.b64decode(buffer.encode(
            'utf-8')))
        config_json += r['Plaintext'].decode('utf-8')
    return json.loads(config_json)

CONFIG = load_config()
'''.format(encrypted_config)
    output_file.write(rendered_config)


def parse_args(args):
    parser = argparse.ArgumentParser(
        prog='fleece config',
        description=('Configuration management')
    )
    parser.add_argument('--config', '-c', default='config.yml',
                        help='Config file (default is config.yml)')
    parser.add_argument('--username', '-u', type=str,
                        default=os.environ.get('RS_USERNAME'),
                        help=('Rackspace username. Can also be set via '
                              'RS_USERNAME environment variable'))
    parser.add_argument('--apikey', '-k', type=str,
                        default=os.environ.get('RS_API_KEY'),
                        help=('Rackspace API key. Can also be set via '
                              'RS_API_KEY environment variable'))
    parser.add_argument('--environments', '-e', type=str,
                        default='./environments.yml',
                        help=('Path to YAML config file with defined accounts '
                              'and environment names. Defaults to '
                              './environments.yml'))
    subparsers = parser.add_subparsers(help='Sub-command help')

    import_parser = subparsers.add_parser(
        'import', help='Import configuration from stdin')
    import_parser.set_defaults(func=import_config)

    export_parser = subparsers.add_parser(
        'export', help='Export configuration to stdout')
    export_parser.add_argument('--json', action='store_true',
                               help='Use JSON format (default is YAML)')
    export_parser.set_defaults(func=export_config)

    edit_parser = subparsers.add_parser('edit', help='Edit configuration')
    edit_parser.add_argument('--json', action='store_true',
                             help='Use JSON format (default is YAML)')
    edit_parser.add_argument(
        '--editor', '-e', default=os.environ.get('FLEECE_EDITOR', 'vi'),
        help='Text editor (defaults to $FLEECE_EDITOR, or else "vi")')
    edit_parser.set_defaults(func=edit_config)

    render_parser = subparsers.add_parser(
        'render', help='Render configuration for an environment')
    render_parser.add_argument('--environment', '-e',
                               help=('Environment name (default is the '
                                     'stage name'))
    render_parser.add_argument('--json', action='store_true',
                               help='Use JSON format (default is YAML)')
    render_parser.add_argument('--encrypt', action='store_true',
                               help='Encrypt rendered configuration')
    render_parser.add_argument('--python', action='store_true',
                               help=('Generate Python module with encrypted '
                                     'configuration'))
    render_parser.add_argument('stage', help='Target stage name')
    render_parser.set_defaults(func=render_config)

    return parser.parse_args(args)


def main(args):
    parsed_args = parse_args(args)

    STATE['awscreds'] = AWSCredentialCache(rs_username=parsed_args.username,
                                           rs_api_key=parsed_args.apikey,
                                           env_config=parsed_args.environments)
    parsed_args.func(parsed_args)
