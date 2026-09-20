# pragma: no cover

import asyncio
import base64
import hashlib
import json
import logging
import time
from typing import Any, List, Optional, cast

import rclpy
import rclpy.client
import rclpy.qos
from builtin_interfaces.msg import Time as RosTime
from fastapi import HTTPException
from rclpy.subscription import Subscription
from rmf_building_map_msgs.msg import AffineImage as RmfAffineImage
from rmf_building_map_msgs.msg import BuildingMap as RmfBuildingMap
from rmf_building_map_msgs.msg import Graph as RmfNavGraph
from rmf_building_map_msgs.msg import Level as RmfLevel
from rmf_dispenser_msgs.msg import DispenserState as RmfDispenserState
from rmf_door_msgs.msg import DoorMode as RmfDoorMode
from rmf_door_msgs.msg import DoorRequest as RmfDoorRequest
from rmf_door_msgs.msg import DoorState as RmfDoorState
from rmf_fleet_msgs.msg import ClosedLanes as RmfClosedLanes
from rmf_fleet_msgs.msg import FleetState as RmfFleetState
from rmf_fleet_msgs.msg import LaneRequest as RmfLaneRequest
from rmf_ingestor_msgs.msg import IngestorState as RmfIngestorState
from rmf_lift_msgs.msg import LiftRequest as RmfLiftRequest
from rmf_lift_msgs.msg import LiftState as RmfLiftState
from rmf_task_msgs.srv import CancelTask as RmfCancelTask
from rmf_task_msgs.srv import SubmitTask as RmfSubmitTask
from rosidl_runtime_py.convert import message_to_ordereddict
from std_msgs.msg import String as RosString

from . import cordon, lane_closures, robot_releases
from .logger import logger as base_logger
from .models import BuildingMap, DispenserState, DoorState, IngestorState, LiftState
from .repositories import CachedFilesRepository, cached_files_repo
from .rmf_io import rmf_events
from .ros import ros_node


def process_building_map(
    rmf_building_map: RmfBuildingMap,
    cached_files: CachedFilesRepository,
) -> BuildingMap:
    """
    1. Converts a `BuildingMap` message to an ordered dict.
    2. Saves the images into `{cache_directory}/{map_name}/`.
    3. Change the `AffineImage` `data` field to the url of the image.
    """
    processed_map = message_to_ordereddict(rmf_building_map)

    for i, level in enumerate(rmf_building_map.levels):
        level: RmfLevel
        for j, image in enumerate(level.images):
            image = cast(RmfAffineImage, image)
            # look at non-crypto hashes if we need more performance
            sha1_hash = hashlib.sha1()
            sha1_hash.update(image.data)
            fingerprint = base64.b32encode(sha1_hash.digest()).lower().decode()
            relpath = f"{rmf_building_map.name}/{level.name}-{image.name}.{fingerprint}.{image.encoding}"  # pylint: disable=line-too-long
            urlpath = cached_files.add_file(cast(bytes, image.data), relpath)
            processed_map["levels"][i]["images"][j]["data"] = urlpath
    return BuildingMap(**processed_map)


class RmfGateway:
    def __init__(
        self,
        cached_files: CachedFilesRepository,
        *,
        logger: Optional[logging.Logger] = None,
    ):
        self._door_req = ros_node().create_publisher(
            RmfDoorRequest, "adapter_door_requests", 10
        )

        transient_qos = rclpy.qos.QoSProfile(
            history=rclpy.qos.HistoryPolicy.KEEP_LAST,
            depth=100,
            reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
            durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
        )

        self._adapter_lift_req = ros_node().create_publisher(
            RmfLiftRequest, "adapter_lift_requests", transient_qos
        )
        # F-339: the operator's cordon, LATCHED — a fleet adapter that
        # starts after the closure was made hears it on discovery, before
        # it admits a robot. The api-server is the only writer.
        # ONE sample of history, deliberately: each message carries the
        # whole intended set, and a late joiner must hear only the LATEST.
        # With the shared depth-100 profile the adapter that restarted
        # after a cordon was lifted replayed the old "close [16, 17]"
        # first and admitted robots under a cordon that no longer existed
        # (drill_cordon_persist run 2, f1-n29).
        self._lane_req = ros_node().create_publisher(
            RmfLaneRequest,
            "lane_closure_requests",
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        lane_closures.set_publisher(self._publish_lane_request)
        # FR-42 (f): the release store, LATCHED by its one writer — the
        # fleet adapter reads it before it admits a robot (the F-339
        # shape). One sample of history: each message carries the whole
        # released set and a late joiner must hear only the latest.
        self._robot_releases = ros_node().create_publisher(
            RosString,
            "gf_robot_releases",
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        robot_releases.set_publisher(self._publish_robot_releases)
        self._submit_task_srv = ros_node().create_client(RmfSubmitTask, "submit_task")
        self._cancel_task_srv = ros_node().create_client(RmfCancelTask, "cancel_task")

        self.cached_files = cached_files
        self.logger = logger or base_logger.getChild(self.__class__.__name__)
        self._subscriptions: List[Subscription] = []

        self._subscribe_all()

    @property
    def cancel_task_client(self) -> rclpy.client.Client:
        """F-285: the dispatcher's CancelTask service — the only door for
        a task still in its bidding queue."""
        return self._cancel_task_srv

    async def call_service(self, client: rclpy.client.Client, req, timeout=1) -> Any:
        """
        Utility to wrap a ros service call in an awaitable,
        raises HTTPException if service call fails.
        """
        fut = client.call_async(req)
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError as e:
            raise HTTPException(503, "ros service call timed out") from e

    def _subscribe_all(self):
        door_states_sub = ros_node().create_subscription(
            RmfDoorState,
            "door_states",
            lambda msg: rmf_events.door_states.on_next(DoorState.from_orm(msg)),
            10,
        )
        self._subscriptions.append(door_states_sub)

        def convert_lift_state(lift_state: RmfLiftState):
            dic = message_to_ordereddict(lift_state)
            return LiftState(**dic)

        lift_states_sub = ros_node().create_subscription(
            RmfLiftState,
            "lift_states",
            lambda msg: rmf_events.lift_states.on_next(
                convert_lift_state(cast(RmfLiftState, msg))
            ),
            10,
        )
        self._subscriptions.append(lift_states_sub)

        dispenser_states_sub = ros_node().create_subscription(
            RmfDispenserState,
            "dispenser_states",
            lambda msg: rmf_events.dispenser_states.on_next(
                DispenserState.from_orm(msg)
            ),
            10,
        )
        self._subscriptions.append(dispenser_states_sub)

        ingestor_states_sub = ros_node().create_subscription(
            RmfIngestorState,
            "ingestor_states",
            lambda msg: rmf_events.ingestor_states.on_next(IngestorState.from_orm(msg)),
            10,
        )
        self._subscriptions.append(ingestor_states_sub)

        map_sub = ros_node().create_subscription(
            RmfBuildingMap,
            "map",
            lambda msg: rmf_events.building_map.on_next(
                process_building_map(cast(RmfBuildingMap, msg), self.cached_files)
            ),
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_ALL,
                depth=1,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._subscriptions.append(map_sub)

        # FR-9d/D-58 (F-259): live mutex-zone occupancy from the fleet
        # adapter — holder, waiters and the bodies physically inside each
        # polygon. JSON over std_msgs/String, the same transport the D-24
        # evacuation control uses in the other direction; on the Humble
        # pin rmf_fleet_msgs/RobotState carries no mutex_groups field to
        # put it in (see ZONE_MANAGER_AUDIT.md §6.5 for the pin-bump
        # replacement). TRANSIENT_LOCAL so a restarted api-server has the
        # aisle state before the next publish rather than a blank map.
        from api_server.routes.zones import on_zone_states  # noqa: PLC0415

        zone_states_sub = ros_node().create_subscription(
            RosString,
            "gf_zone_states",
            lambda msg: on_zone_states(cast(RosString, msg).data),
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._subscriptions.append(zone_states_sub)

        # F-268: the per-robot position CLOCK. `location.t` exists only
        # on the ROS message — the RMF API fleet state carries a single
        # fleet-wide `unix_millis_time`, identical for every robot, which
        # cannot tell a current pose from one RMF stopped accepting
        # updates for. BEST_EFFORT to match the publisher; this feed is
        # ~10 Hz and a dropped sample costs nothing.
        from api_server.routes.fleets import on_fleet_positions  # noqa: PLC0415

        fleet_positions_sub = ros_node().create_subscription(
            RmfFleetState,
            "fleet_states",
            lambda msg: on_fleet_positions(cast(RmfFleetState, msg)),
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=10,
                reliability=rclpy.qos.ReliabilityPolicy.BEST_EFFORT,
            ),
        )
        self._subscriptions.append(fleet_positions_sub)

        # F-332/F-333: the operator's cordon, so a mission into it is
        # REFUSED before it is queued rather than accepted and silently
        # never delivered.
        #
        # Both feeds are read, and they must be read together: the lane
        # indices in `/closed_lanes` are meaningless except against the
        # fleet's own `/nav_graphs`, which is NOT the derived graph the
        # F-111 guard uses and NOT the authored building map (F-333 has
        # the measurement — the same index 16 names three different
        # pieces of map). TRANSIENT_LOCAL on both, matching the fleet
        # adapter's publishers, so a restarted api-server knows the
        # cordon before the next closure rather than after it.
        def _on_nav_graph(msg):
            cordon.on_nav_graph(cast(RmfNavGraph, msg))
            # F-339: a fleet that has just (re)started publishes its graph;
            # answer with the cordon it must honour
            lane_closures.on_graph(str(msg.name))
            # FR-42: ...and with the released set it admits from
            robot_releases.on_graph(str(msg.name))

        def _on_closed_lanes(msg):
            cordon.on_closed_lanes(cast(RmfClosedLanes, msg))
            # F-339: the fleet's report is the CONFIRMATION; a report that
            # lacks an intended lane (an adapter reporting [] after a
            # restart) is answered with the intent, re-asserted
            lane_closures.on_fleet_confirmation(
                str(msg.fleet_name),
                frozenset(int(i) for i in msg.closed_lanes),
                int(time.time() * 1000),
            )

        nav_graph_sub = ros_node().create_subscription(
            RmfNavGraph,
            "nav_graphs",
            _on_nav_graph,
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=10,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._subscriptions.append(nav_graph_sub)

        closed_lanes_sub = ros_node().create_subscription(
            RmfClosedLanes,
            "closed_lanes",
            _on_closed_lanes,
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=10,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._subscriptions.append(closed_lanes_sub)

        # F-338: who charges where, from the fleet adapter (the one
        # authority on it — the api-server has no fleet config). Latched.
        def _on_chargers(msg):
            try:
                payload = json.loads(msg.data)
                cordon.on_chargers(str(payload.get("fleet") or ""),
                                   payload.get("chargers") or {})
            except Exception:  # a malformed message must not take us down
                pass

        # FR-42: the adapter's commissioning status — every configured
        # robot's release/admission state and the live readiness facts of
        # the ones the fleet may not command. Latched at 1 Hz by the
        # adapter; the release route judges FR-42 (d) against it.
        watch_only_sub = ros_node().create_subscription(
            RosString,
            "gf_watch_only",
            lambda msg: robot_releases.on_watch_only(cast(RosString, msg).data),
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._subscriptions.append(watch_only_sub)

        chargers_sub = ros_node().create_subscription(
            RosString,
            "gf_chargers",
            _on_chargers,
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=10,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._subscriptions.append(chargers_sub)

    def _publish_robot_releases(self, fleet: str, payload: dict) -> None:
        self._robot_releases.publish(RosString(data=json.dumps(payload)))

    def _publish_lane_request(
        self, fleet: str, close: List[int], open_: List[int]
    ) -> None:
        self._lane_req.publish(
            RmfLaneRequest(
                fleet_name=fleet,
                close_lanes=[int(i) for i in close],
                open_lanes=[int(i) for i in open_],
            )
        )

    @staticmethod
    def now() -> Optional[RosTime]:
        """
        Returns the current sim time, or `None` if not using sim time
        """
        return ros_node().get_clock().now().to_msg()

    def request_door(self, door_name: str, mode: int) -> None:
        msg = RmfDoorRequest(
            door_name=door_name,
            request_time=ros_node().get_clock().now().to_msg(),
            requester_id=ros_node().get_name(),  # FIXME: use username
            requested_mode=RmfDoorMode(
                value=mode,
            ),
        )
        self._door_req.publish(msg)

    def request_lift(
        self, lift_name: str, destination: str, request_type: int, door_mode: int
    ):
        msg = RmfLiftRequest(
            lift_name=lift_name,
            request_time=ros_node().get_clock().now().to_msg(),
            session_id=ros_node().get_name(),
            request_type=request_type,
            destination_floor=destination,
            door_state=door_mode,
        )
        self._adapter_lift_req.publish(msg)


_rmf_gateway: RmfGateway


def rmf_gateway() -> RmfGateway:
    return _rmf_gateway


def startup():
    """
    Starts subscribing to all ROS topics.
    Must be called after the ros node is created and before spinning the it.
    """
    global _rmf_gateway
    _rmf_gateway = RmfGateway(cached_files_repo)
    return _rmf_gateway
