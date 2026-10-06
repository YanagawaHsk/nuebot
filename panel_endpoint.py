"""Separate local administration from the existing hosted SSH control origin."""
import os

def configuration(profile):
    if profile not in ('default','local'):
        raise ValueError('Unknown panel profile; use default or local')
    port=5102 if profile=='local' else 5100
    return {'port':port,'bridge_port':port+1,'origin':f'http://127.0.0.1:{port}',
            'bridge_origin':f'http://127.0.0.1:{port+1}',
            'cookie':'nue_session_local' if profile=='local' else 'nue_session'}

PROFILE=os.environ.get('NUEBOT_PANEL_PROFILE','default')
VALUE=configuration(PROFILE)
PORT=VALUE['port']
BRIDGE_PORT=VALUE['bridge_port']
ORIGIN=VALUE['origin']
BRIDGE_ORIGIN=VALUE['bridge_origin']
COOKIE=VALUE['cookie']
