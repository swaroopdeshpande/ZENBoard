"""
Amazon Gmail Blueprint
Handles Gmail OAuth token storage and verification
"""

import json
import logging
from pathlib import Path
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

amazon_bp = Blueprint('amazon', __name__, url_prefix='/amazon')

TOKEN_DIR = Path("/home/zenith/InkyPi/src/plugins/amazon_order_tracker/.cache")
TOKEN_DIR.mkdir(exist_ok=True)
TOKEN_FILE = TOKEN_DIR / "gmail_token.json"


@amazon_bp.route('/save-gmail-token', methods=['POST'])
def save_gmail_token():
    """Save Gmail OAuth token."""
    try:
        data = request.get_json()
        token_data = data.get('token')

        if not token_data:
            return jsonify({
                'success': False,
                'error': 'No token provided'
            })

        # Validate token has required fields
        required_fields = ['token', 'refresh_token', 'token_uri', 'client_id', 'client_secret']
        for field in required_fields:
            if field not in token_data:
                return jsonify({
                    'success': False,
                    'error': f'Missing required field: {field}'
                })

        # Save token to file
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f)

        # Set readable file permissions (token needs to be readable by inkypi service)
        import os
        os.chmod(TOKEN_FILE, 0o644)

        logger.info("Gmail token saved successfully")

        return jsonify({
            'success': True,
            'message': 'Gmail token saved'
        })

    except Exception as e:
        logger.error(f"Save token error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@amazon_bp.route('/gmail-status', methods=['GET'])
def gmail_status():
    """Check if Gmail token is configured."""
    try:
        if TOKEN_FILE.exists():
            with open(TOKEN_FILE, 'r') as f:
                token_data = json.load(f)

            # Validate has token
            if token_data.get('token'):
                return jsonify({
                    'connected': True,
                    'has_refresh': bool(token_data.get('refresh_token'))
                })

        return jsonify({
            'connected': False
        })

    except Exception as e:
        logger.error(f"Status check error: {e}")
        return jsonify({
            'connected': False,
            'error': str(e)
        })


@amazon_bp.route('/clear-gmail-token', methods=['POST'])
def clear_gmail_token():
    """Clear saved Gmail token."""
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        logger.info("Gmail token cleared")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Clear token error: {e}")
        return jsonify({'success': False, 'error': str(e)})


def register_amazon_routes(app):
    """Register Amazon routes with Flask app."""
    app.register_blueprint(amazon_bp)
    logger.info("Amazon Gmail routes registered")
