from http import HTTPStatus
from typing import Annotated, Any, Optional, Union
from langflow.services.auth.utils import api_key_security, get_current_active_user


from langflow.services.cache.utils import save_uploaded_file
from langflow.services.database.models.flow import Flow
from langflow.processing.process import process_graph_cached, process_tweaks
from langflow.services.database.models.user.user import User
from langflow.services.utils import get_settings_service, get_task_service
from loguru import logger
from fastapi import APIRouter, Depends, HTTPException, UploadFile, Body, status
import sqlalchemy as sa
from langflow.interface.custom.custom_component import CustomComponent


from langflow.api.v1.schemas import (
    ProcessResponse,
    TaskStatusResponse,
    UploadFileResponse,
    CustomComponentCode,
)

from langflow.api.utils import merge_nested_dicts_with_renaming

from langflow.interface.types import (
    build_langchain_types_dict,
    build_langchain_template_custom_component,
    build_langchain_custom_component_list_from_path,
)

from langflow.services.utils import get_session

try:
    from langflow.worker import process_graph_cached_task
except ImportError:

    def process_graph_cached_task(*args, **kwargs):
        raise NotImplementedError("Celery is not installed")


from sqlmodel import Session


from langflow.services.task.manager import TaskService

# build router
router = APIRouter(tags=["Base"])


@router.get("/all", dependencies=[Depends(get_current_active_user)])
def get_all(
    settings_service=Depends(get_settings_service),
):
    logger.debug("Building langchain types dict")
    native_components = build_langchain_types_dict()
    # custom_components is a list of dicts
    # need to merge all the keys into one dict
    custom_components_from_file: dict[str, Any] = {}
    if settings_service.settings.COMPONENTS_PATH:
        logger.info(
            f"Building custom components from {settings_service.settings.COMPONENTS_PATH}"
        )

        custom_component_dicts = []
        processed_paths = []
        for path in settings_service.settings.COMPONENTS_PATH:
            if str(path) in processed_paths:
                continue
            custom_component_dict = build_langchain_custom_component_list_from_path(
                str(path)
            )
            custom_component_dicts.append(custom_component_dict)
            processed_paths.append(str(path))

        logger.info(f"Loading {len(custom_component_dicts)} category(ies)")
        for custom_component_dict in custom_component_dicts:
            # custom_component_dict is a dict of dicts
            if not custom_component_dict:
                continue
            category = list(custom_component_dict.keys())[0]
            logger.info(
                f"Loading {len(custom_component_dict[category])} component(s) from category {category}"
            )
            custom_components_from_file = merge_nested_dicts_with_renaming(
                custom_components_from_file, custom_component_dict
            )

    return merge_nested_dicts_with_renaming(
        native_components, custom_components_from_file
    )


# For backwards compatibility we will keep the old endpoint
@router.post(
    "/predict/{flow_id}",
    response_model=ProcessResponse,
    dependencies=[Depends(api_key_security)],
)
@router.post(
    "/process/{flow_id}",
    response_model=ProcessResponse,
)
async def process_flow(
    session: Annotated[Session, Depends(get_session)],
    flow_id: str,
    inputs: Optional[dict] = None,
    tweaks: Optional[dict] = None,
    clear_cache: Annotated[bool, Body(embed=True)] = False,  # noqa: F821
    session_id: Annotated[Union[None, str], Body(embed=True)] = None,  # noqa: F821
    task_service: "TaskService" = Depends(get_task_service),
    api_key_user: User = Depends(api_key_security),
    sync: Annotated[bool, Body(embed=True)] = True,  # noqa: F821
):
    """
    Endpoint to process an input with a given flow_id.
    """

    try:
        if api_key_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API Key",
            )

        # Get the flow that matches the flow_id and belongs to the user
        flow = (
            session.query(Flow)
            .filter(Flow.id == flow_id)
            .filter(Flow.user_id == api_key_user.id)
            .first()
        )
        if flow is None:
            raise ValueError(f"Flow {flow_id} not found")

        if flow.data is None:
            raise ValueError(f"Flow {flow_id} has no data")
        graph_data = flow.data
        if tweaks:
            try:
                graph_data = process_tweaks(graph_data, tweaks)
            except Exception as exc:
                logger.error(f"Error processing tweaks: {exc}")
        if sync:
            task_id, result = await task_service.launch_and_await_task(
                process_graph_cached_task
                if task_service.use_celery
                else process_graph_cached,
                graph_data,
                inputs,
                clear_cache,
                session_id,
            )
            if isinstance(result, dict) and "result" in result:
                task_result = result["result"]
                session_id = result["session_id"]
            else:
                task_result = result.result

                session_id = result.session_id
        else:
            task_id, task = await task_service.launch_task(
                process_graph_cached_task
                if task_service.use_celery
                else process_graph_cached,
                graph_data,
                inputs,
                clear_cache,
                session_id,
            )
            task_result = task.status
        return ProcessResponse(result=task_result, id=task_id, session_id=session_id)
    except sa.exc.StatementError as exc:
        # StatementError('(builtins.ValueError) badly formed hexadecimal UUID string')
        if "badly formed hexadecimal UUID string" in str(exc):
            # This means the Flow ID is not a valid UUID which means it can't find the flow
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
    except ValueError as exc:
        if f"Flow {flow_id} not found" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc
    except Exception as e:
        # Log stack trace
        logger.exception(e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/task/{task_id}/status", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    task_service = get_task_service()
    task = task_service.get_task(task_id)
    result = None
    if task.ready():
        result = task.result
        if isinstance(result, dict) and "result" in result:
            result = result["result"]
        elif hasattr(result, "result"):
            result = result.result

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatusResponse(status=task.status, result=result)


@router.post(
    "/upload/{flow_id}",
    response_model=UploadFileResponse,
    status_code=HTTPStatus.CREATED,
)
async def create_upload_file(file: UploadFile, flow_id: str):
    # Cache file
    try:
        file_path = save_uploaded_file(file.file, folder_name=flow_id)

        return UploadFileResponse(
            flowId=flow_id,
            file_path=file_path,
        )
    except Exception as exc:
        logger.error(f"Error saving file: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# get endpoint to return version of langflow
@router.get("/version")
def get_version():
    from langflow import __version__

    return {"version": __version__}


@router.post("/custom_component", status_code=HTTPStatus.OK)
async def custom_component(
    raw_code: CustomComponentCode,
):
    extractor = CustomComponent(code=raw_code.code)
    extractor.is_check_valid()

    return build_langchain_template_custom_component(extractor)
