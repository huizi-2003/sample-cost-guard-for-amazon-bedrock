"""Pytest-wide isolation from developers' real AWS credentials."""

import os

# Install deterministic fake credentials before test modules import boto3. Any
# missed AWS mock then fails against a non-production identity instead of using
# ~/.aws credentials or the EC2 instance metadata credential chain.
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
os.environ['AWS_SESSION_TOKEN'] = 'testing'
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_SHARED_CREDENTIALS_FILE'] = os.devnull
os.environ['AWS_CONFIG_FILE'] = os.devnull
os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'

import pytest  # noqa: E402

from common import config as common_config  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_account_id_cache():
    """Prevent one test's STS result or mock from hiding another test's leak."""
    common_config._account_id = None
    yield
    common_config._account_id = None
