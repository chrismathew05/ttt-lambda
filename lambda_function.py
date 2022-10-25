import json
import boto3
import os

from config import _WINNING_COMBOS


def scan_conns(conns: list) -> tuple:
    """Extracts game info from output of connections table.

    :param conns: list of rows from connections table
    :return: tuple of
        - avail (available player types)
        - p1_id (id of player one if active else "")
        - p2_id (id of player two if active else "")
        - p1_moves (list of moves already made by player one)
        - p2_moves (list of moves already made by player two)
    """
    p1_id, p2_id = "", ""
    p1_moves, p2_moves = [], []

    for conn in conns:
        # player one moves
        if conn["connType"][0] == "0":
            p1_id = conn["connectionId"]
            p1_moves_str = conn["connType"].split(";")[1]
            if p1_moves_str:
                p1_moves = p1_moves_str.split(",")

        # player two moves
        elif conn["connType"][0] == "1":
            p2_id = conn["connectionId"]
            p2_moves_str = conn["connType"].split(";")[1]
            if p2_moves_str:
                p2_moves = p2_moves_str.split(",")

    avail = []
    if not p1_id:
        avail.append(0)

    if not p2_id:
        avail.append(1)
    avail.append(2)

    return avail, p1_id, p2_id, p1_moves, p2_moves


def lambda_handler(event: dict, context: dict) -> dict:
    """Starting point for AWS lambda call

    :param event: JSON doc containing data for lambda function to process
    :param context: Provides info about invocation, function and runtime env
    :return: Response object containing dictionary of status/results
    """

    print(event)

    # obtain route, connection and message info
    client = boto3.client(
        "apigatewaymanagementapi", endpoint_url=os.environ.get("CONNECTION_URL")
    )
    route_key = event["requestContext"]["routeKey"]
    connection_id = event["requestContext"]["connectionId"]
    if "body" in event:
        body = json.loads(event["body"])
        if "message" in body:
            message = body["message"]

    # obtain player info/moves from DB
    tbl = boto3.resource("dynamodb").Table("connections")

    # ROUTE: client connects to websocket (opens app)
    if route_key == "$connect":
        # connType: 2 (spectator - default), 1 (player two), 0 (player one)
        tbl.put_item(Item={"connectionId": connection_id, "connType": "2"})

    else:
        # ROUTE: client disconnects from websocket (closes window)
        if route_key == "$disconnect":
            # remove connection record
            tbl.delete_item(Key={"connectionId": connection_id})

        # obtain updated game info after possible connection changes
        conns = tbl.scan()["Items"]
        avail, p1_id, p2_id, p1_moves, p2_moves = scan_conns(conns)
        data = {
            "avail": avail,
            "p1Id": p1_id,
            "p2Id": p2_id,
            "p1Moves": p1_moves,
            "p2Moves": p2_moves,
            "newMessage": {},
        }

        # ROUTE: client joins game as player 1/2 (clicks Join Game)
        if route_key == "joinGame":
            # update connection type if availability exists
            if (0 in avail) or (1 in avail):
                tbl.update_item(
                    Key={"connectionId": connection_id},
                    UpdateExpression=f"SET connType = :t",
                    ExpressionAttributeValues={":t": f"{avail[0]};"},
                )

                p_id = avail.pop(0)
                if p_id == 0:
                    data["p1Id"] = connection_id
                else:
                    data["p2Id"] = connection_id
                data["avail"] = avail

        # ROUTE: client makes a tic-tac-toe play (clicks square)
        elif route_key == "makePlay":
            # ensure connection id eligible to make play
            if (connection_id != p1_id) and (connection_id != p2_id):
                return {
                    "isBase64Encoded": False,
                    "statusCode": 200,
                    "headers": {"status": "Failure"},
                    "body": "User ineligible to make play.",
                }

            # ensure play has not been made already
            if (message in data["p1Moves"]) or (message in data["p2Moves"]):
                return {
                    "isBase64Encoded": False,
                    "statusCode": 200,
                    "headers": {"status": "Failure"},
                    "body": "Move already made!",
                }

            # update moves recorded for player 1 and 2
            check_arr = []
            if connection_id == p1_id:
                data["p1Moves"].append(message)
                moves_str = f"0;{','.join(data['p1Moves'])}"
                check_arr = data["p1Moves"]
            if connection_id == p2_id:
                data["p2Moves"].append(message)
                moves_str = f"1;{','.join(data['p2Moves'])}"
                check_arr = data["p2Moves"]

            # determine game outcome at this time
            check_set = set([int(x) for x in check_arr])
            decision = ""
            if len(check_set) >= 3:

                # win check
                for combo in _WINNING_COMBOS:
                    if set(combo).issubset(check_set):
                        decision = "won"
                        break

                # tie check
                if (
                    decision != "won"
                    and (len(data["p1Moves"]) + len(data["p2Moves"])) == 9
                ):
                    decision = "tied"

                if decision != "":
                    # reset game and send message to chat
                    winner = connection_id
                    loser = p2_id if p1_id == winner else p1_id

                    # make p1 and p2 spectators
                    tbl.update_item(
                        Key={"connectionId": p1_id},
                        UpdateExpression=f"SET connType = :t",
                        ExpressionAttributeValues={":t": "2"},
                    )
                    tbl.update_item(
                        Key={"connectionId": p2_id},
                        UpdateExpression=f"SET connType = :t",
                        ExpressionAttributeValues={":t": "2"},
                    )

                    # clear game info
                    data = {
                        "avail": [0, 1, 2],
                        "p1Id": "",
                        "p2Id": "",
                        "p1Moves": [],
                        "p2Moves": [],
                        "newMessage": {
                            "senderId": "GAME",
                            "chatMessage": f"{winner} has {decision} against {loser}!",
                        },
                    }

            # no decision - game goes on; update db
            if decision == "":
                tbl.update_item(
                    Key={"connectionId": connection_id},
                    UpdateExpression=f"SET connType = :t",
                    ExpressionAttributeValues={":t": moves_str},
                )

        # ROUTE: client sends message to chat
        elif route_key == "sendMessage":
            # update data packet
            data["newMessage"] = {"senderId": connection_id, "chatMessage": message}

        # BROADCAST updated data to all clients
        for conn in conns:
            conn_id = conn["connectionId"]
            data["connectionId"] = conn_id
            client.post_to_connection(
                ConnectionId=conn_id, Data=json.dumps(data).encode("utf-8")
            )

    return {
        "isBase64Encoded": False,
        "statusCode": 200,
        "headers": {"status": "Success"},
        "body": "Lambda executed successfully.",
    }
