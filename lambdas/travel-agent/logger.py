"""
Single project logger. Returns an aws-lambda-powertools Logger so every line
emitted from this Lambda is structured JSON with `service`, `function_name`,
`function_request_id`, etc. — making CloudWatch Logs Insights queries trivial.

Powertools Logger inherits from the stdlib Logger, so existing call sites
(`l.info(...)`, `l.error(..., exc_info=True)`, `l.exception(e)`) keep working
unchanged. Extra fields can be passed via `extra={...}` and will appear as
top-level JSON keys.
"""

from aws_lambda_powertools import Logger


def get():
    return Logger(service="travel-agent")
