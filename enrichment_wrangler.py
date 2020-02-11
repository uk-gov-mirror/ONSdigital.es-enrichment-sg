import json
import logging
import os

import boto3
from botocore.exceptions import ClientError, IncompleteReadError
from es_aws_functions import aws_functions, exception_classes
from marshmallow import Schema, fields


class EnvironSchema(Schema):
    checkpoint = fields.Str(required=True)
    bucket_name = fields.Str(required=True)
    identifier_column = fields.Str(required=True)
    incoming_message_group = fields.Str(required=True)
    method_name = fields.Str(required=True)
    out_file_name = fields.Str(required=True)
    sns_topic_arn = fields.Str(required=True)
    sqs_message_group_id = fields.Str(required=True)
    period_column = fields.Str(required=True)


def lambda_handler(event, context):
    """
    Lambda function preparing data for enrichment and then calling the enrichment method.
    :param event: Json string representing input - String
    :param context:
    :return Json: success and checkpoint information, and/or indication of error message.
    """

    # Set up logger.
    current_module = "Enrichment - Wrangler"
    error_message = ''
    log_message = ''
    logger = logging.getLogger("Enrichment")
    logger.setLevel(10)

    # Define run_id outside of try block
    run_id = 0
    try:
        logger.info("Enrichment Wrangler Begun")
        # Retrieve run_id before input validation
        # Because it is used in exception handling
        run_id = event['RuntimeVariables']['run_id']

        schema = EnvironSchema()
        config, errors = schema.load(os.environ)
        if errors:
            logger.error(f"Error validating environment params: {errors}")
            raise ValueError(f"Error validating environment params: {errors}")

        logger.info("Validated parameters.")

        # Environment Variables.
        checkpoint = int(config["checkpoint"])
        bucket_name = config["bucket_name"]
        identifier_column = config['identifier_column']
        incoming_message_group = config["incoming_message_group"]
        method_name = config["method_name"]
        out_file_name = config["out_file_name"]
        period_column = config['period_column']
        sns_topic_arn = config["sns_topic_arn"]
        sqs_message_group_id = config["sqs_message_group_id"]

        # Runtime Variables.
        lookups = event['RuntimeVariables']['lookups']
        survey_column = event['RuntimeVariables']["survey_column"]
        sqs_queue_url = event['RuntimeVariables']["queue_url"]
        in_file_name = event['RuntimeVariables']["in_file_name"]['enrichment']
        marine_mismatch_check = event['RuntimeVariables']["marine_mismatch_check"]

        logger.info("Retrieved configuration variables.")

        # Set up client.
        lambda_client = boto3.client("lambda", region_name="eu-west-2")
        sqs = boto3.client("sqs", region_name='eu-west-2')
        data_df, receipt_handler = aws_functions.get_dataframe(sqs_queue_url, bucket_name,
                                                               in_file_name,
                                                               incoming_message_group,
                                                               run_id)

        logger.info("Retrieved data from s3")
        data_json = data_df.to_json(orient="records")
        json_payload = {
            "RuntimeVariables": {
                "data": data_json,
                "lookups": lookups,
                "marine_mismatch_check": marine_mismatch_check,
                "survey_column": survey_column,
                "period_column": period_column,
                "identifier_column": identifier_column
            }
        }
        response = lambda_client.invoke(
            FunctionName=method_name,
            Payload=json.dumps(json_payload)
        )

        logger.info("Successfully invoked method.")
        json_response = json.loads(response.get("Payload").read().decode("utf-8"))
        logger.info("JSON extracted from method response.")

        if not json_response['success']:
            raise exception_classes.MethodFailure(json_response['error'])

        anomalies = json_response["anomalies"]

        aws_functions.save_data(bucket_name, out_file_name, json_response["data"],
                                sqs_queue_url, sqs_message_group_id, run_id)

        logger.info("Successfully sent data to s3.")

        aws_functions.send_sns_message_with_anomalies(checkpoint, anomalies,
                                                      sns_topic_arn, "Enrichment.")
        if receipt_handler:
            sqs.delete_message(QueueUrl=sqs_queue_url, ReceiptHandle=receipt_handler)
        logger.info("Successfully sent message to sns.")
        checkpoint += 1

    # Raise value validation error.
    except ValueError as e:
        error_message = "Parameter Validation Error in " + current_module \
                        + " |- " + str(e.args) + " | Request ID: " \
                        + str(context.aws_request_id) \
                        + " | Run_id: " + str(run_id)
        log_message = error_message + " | Line: " + str(e.__traceback__.tb_lineno)
    # Raise client based error.
    except ClientError as e:
        error_message = "AWS Error (" + str(e.response['Error']['Code']) \
                        + ") " + current_module + " |- " + str(e.args) \
                        + " | Request ID: " + str(context.aws_request_id)\
                        + " | Run_id: " + str(run_id)
        log_message = error_message + " | Line: " + str(e.__traceback__.tb_lineno)
    # Raise key/index error.
    except KeyError as e:
        error_message = "Key Error in " + current_module + " |- " + \
                        str(e.args) + " | Request ID: " \
                        + str(context.aws_request_id)\
                        + " | Run_id: " + str(run_id)
        log_message = error_message + " | Line: " + str(e.__traceback__.tb_lineno)
    # Raise error in lambda response.
    except IncompleteReadError as e:
        error_message = "Incomplete Lambda response encountered in " \
                        + current_module + " |- " + \
                        str(e.args) + " | Request ID: " \
                        + str(context.aws_request_id)\
                        + " | Run_id: " + str(run_id)
        log_message = error_message + " | Line: " + str(e.__traceback__.tb_lineno)
    # Raise the Method Failing.
    except exception_classes.MethodFailure as e:
        error_message = e.error_message
        log_message = "Error in " + method_name + "."\
            + " | Run_id: " + str(run_id)
    # Raise a general exception.
    except Exception as e:
        error_message = "General Error in " + current_module + \
                        " (" + str(type(e)) + ") |- " + str(e.args) + \
                        " | Request ID: " + str(context.aws_request_id)\
                        + " | Run_id: " + str(run_id)
        log_message = error_message + " | Line: " + str(e.__traceback__.tb_lineno)
    finally:
        if (len(error_message)) > 0:
            logger.error(log_message)
            raise exception_classes.LambdaFailure(error_message)

    logger.info("Successfully completed module: " + current_module)
    return {"success": True, "checkpoint": checkpoint}
