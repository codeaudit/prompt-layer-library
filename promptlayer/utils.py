import asyncio
import contextvars
import datetime
import functools
import json
import os
import sys
import types
from copy import deepcopy
from enum import Enum
from typing import Callable, Generator, List, Union

import requests
from opentelemetry import context, trace

from promptlayer.types.prompt_template import (
    GetPromptTemplate,
    GetPromptTemplateResponse,
    ListPromptTemplateResponse,
    PublishPromptTemplate,
    PublishPromptTemplateResponse,
)

URL_API_PROMPTLAYER = os.environ.setdefault(
    "URL_API_PROMPTLAYER", "https://api.promptlayer.com"
)


def promptlayer_api_handler(
    function_name,
    provider_type,
    args,
    kwargs,
    tags,
    response,
    request_start_time,
    request_end_time,
    api_key,
    return_pl_id=False,
    llm_request_span_id=None,
):
    if (
        isinstance(response, types.GeneratorType)
        or isinstance(response, types.AsyncGeneratorType)
        or type(response).__name__
        in [
            "Stream",
            "AsyncStream",
            "AsyncMessageStreamManager",
            "MessageStreamManager",
        ]
    ):
        return GeneratorProxy(
            generator=response,
            api_request_arguments={
                "function_name": function_name,
                "provider_type": provider_type,
                "args": args,
                "kwargs": kwargs,
                "tags": tags,
                "request_start_time": request_start_time,
                "request_end_time": request_end_time,
                "return_pl_id": return_pl_id,
                "llm_request_span_id": llm_request_span_id,
            },
            api_key=api_key,
        )
    else:
        request_id = promptlayer_api_request(
            function_name=function_name,
            provider_type=provider_type,
            args=args,
            kwargs=kwargs,
            tags=tags,
            response=response,
            request_start_time=request_start_time,
            request_end_time=request_end_time,
            api_key=api_key,
            return_pl_id=return_pl_id,
            llm_request_span_id=llm_request_span_id,
        )
        if return_pl_id:
            return response, request_id
        return response


async def promptlayer_api_handler_async(
    function_name,
    provider_type,
    args,
    kwargs,
    tags,
    response,
    request_start_time,
    request_end_time,
    api_key,
    return_pl_id=False,
    llm_request_span_id=None,
):
    return await run_in_thread_async(
        None,
        promptlayer_api_handler,
        function_name,
        provider_type,
        args,
        kwargs,
        tags,
        response,
        request_start_time,
        request_end_time,
        api_key,
        return_pl_id=return_pl_id,
        llm_request_span_id=llm_request_span_id,
    )


def convert_native_object_to_dict(native_object):
    if isinstance(native_object, dict):
        return {k: convert_native_object_to_dict(v) for k, v in native_object.items()}
    if isinstance(native_object, list):
        return [convert_native_object_to_dict(v) for v in native_object]
    if isinstance(native_object, Enum):
        return native_object.value
    if hasattr(native_object, "__dict__"):
        return {
            k: convert_native_object_to_dict(v)
            for k, v in native_object.__dict__.items()
        }
    return native_object


def promptlayer_api_request(
    *,
    function_name,
    provider_type,
    args,
    kwargs,
    tags,
    response,
    request_start_time,
    request_end_time,
    api_key,
    return_pl_id=False,
    metadata=None,
    llm_request_span_id=None,
):
    if isinstance(response, dict) and hasattr(response, "to_dict_recursive"):
        response = response.to_dict_recursive()
    request_response = None
    if hasattr(
        response, "dict"
    ):  # added this for anthropic 3.0 changes, they return a completion object
        response = response.dict()
    try:
        request_response = requests.post(
            f"{URL_API_PROMPTLAYER}/track-request",
            json={
                "function_name": function_name,
                "provider_type": provider_type,
                "args": args,
                "kwargs": convert_native_object_to_dict(kwargs),
                "tags": tags,
                "request_response": response,
                "request_start_time": request_start_time,
                "request_end_time": request_end_time,
                "metadata": metadata,
                "api_key": api_key,
                "span_id": llm_request_span_id,
            },
        )
        if not hasattr(request_response, "status_code"):
            warn_on_bad_response(
                request_response,
                "WARNING: While logging your request PromptLayer had the following issue",
            )
        elif request_response.status_code != 200:
            warn_on_bad_response(
                request_response,
                "WARNING: While logging your request PromptLayer had the following error",
            )
    except Exception as e:
        print(
            f"WARNING: While logging your request PromptLayer had the following error: {e}",
            file=sys.stderr,
        )
    if request_response is not None and return_pl_id:
        return request_response.json().get("request_id")


def track_request(**body):
    try:
        response = requests.post(
            f"{URL_API_PROMPTLAYER}/track-request",
            json=body,
        )
        if response.status_code != 200:
            warn_on_bad_response(
                response,
                f"PromptLayer had the following error while tracking your request: {response.text}",
            )
        return response.json()
    except requests.exceptions.RequestException as e:
        print(
            f"WARNING: While logging your request PromptLayer had the following error: {e}",
            file=sys.stderr,
        )
        return {}


def promptlayer_api_request_async(
    function_name,
    provider_type,
    args,
    kwargs,
    tags,
    response,
    request_start_time,
    request_end_time,
    api_key,
    return_pl_id=False,
):
    return run_in_thread_async(
        None,
        promptlayer_api_request,
        function_name=function_name,
        provider_type=provider_type,
        args=args,
        kwargs=kwargs,
        tags=tags,
        response=response,
        request_start_time=request_start_time,
        request_end_time=request_end_time,
        api_key=api_key,
        return_pl_id=return_pl_id,
    )


def promptlayer_get_prompt(
    prompt_name, api_key, version: int = None, label: str = None
):
    """
    Get a prompt from the PromptLayer library
    version: version of the prompt to get, None for latest
    label: The specific label of a prompt you want to get. Setting this will supercede version
    """
    try:
        request_response = requests.get(
            f"{URL_API_PROMPTLAYER}/library-get-prompt-template",
            headers={"X-API-KEY": api_key},
            params={"prompt_name": prompt_name, "version": version, "label": label},
        )
    except Exception as e:
        raise Exception(
            f"PromptLayer had the following error while getting your prompt: {e}"
        )
    if request_response.status_code != 200:
        raise_on_bad_response(
            request_response,
            "PromptLayer had the following error while getting your prompt",
        )

    return request_response.json()


def promptlayer_publish_prompt(
    prompt_name, prompt_template, commit_message, tags, api_key, metadata=None
):
    try:
        request_response = requests.post(
            f"{URL_API_PROMPTLAYER}/library-publish-prompt-template",
            json={
                "prompt_name": prompt_name,
                "prompt_template": prompt_template,
                "commit_message": commit_message,
                "tags": tags,
                "api_key": api_key,
                "metadata": metadata,
            },
        )
    except Exception as e:
        raise Exception(
            f"PromptLayer had the following error while publishing your prompt: {e}"
        )
    if request_response.status_code != 200:
        raise_on_bad_response(
            request_response,
            "PromptLayer had the following error while publishing your prompt",
        )
    return True


def promptlayer_track_prompt(
    request_id, prompt_name, input_variables, api_key, version, label
):
    try:
        request_response = requests.post(
            f"{URL_API_PROMPTLAYER}/library-track-prompt",
            json={
                "request_id": request_id,
                "prompt_name": prompt_name,
                "prompt_input_variables": input_variables,
                "api_key": api_key,
                "version": version,
                "label": label,
            },
        )
        if request_response.status_code != 200:
            warn_on_bad_response(
                request_response,
                "WARNING: While tracking your prompt PromptLayer had the following error",
            )
            return False
    except Exception as e:
        print(
            f"WARNING: While tracking your prompt PromptLayer had the following error: {e}",
            file=sys.stderr,
        )
        return False
    return True


def promptlayer_track_metadata(request_id, metadata, api_key):
    try:
        request_response = requests.post(
            f"{URL_API_PROMPTLAYER}/library-track-metadata",
            json={
                "request_id": request_id,
                "metadata": metadata,
                "api_key": api_key,
            },
        )
        if request_response.status_code != 200:
            warn_on_bad_response(
                request_response,
                "WARNING: While tracking your metadata PromptLayer had the following error",
            )
            return False
    except Exception as e:
        print(
            f"WARNING: While tracking your metadata PromptLayer had the following error: {e}",
            file=sys.stderr,
        )
        return False
    return True


def promptlayer_track_score(request_id, score, score_name, api_key):
    try:
        data = {"request_id": request_id, "score": score, "api_key": api_key}
        if score_name is not None:
            data["name"] = score_name
        request_response = requests.post(
            f"{URL_API_PROMPTLAYER}/library-track-score",
            json=data,
        )
        if request_response.status_code != 200:
            warn_on_bad_response(
                request_response,
                "WARNING: While tracking your score PromptLayer had the following error",
            )
            return False
    except Exception as e:
        print(
            f"WARNING: While tracking your score PromptLayer had the following error: {e}",
            file=sys.stderr,
        )
        return False
    return True


class GeneratorProxy:
    def __init__(self, generator, api_request_arguments, api_key):
        self.generator = generator
        self.results = []
        self.api_request_arugments = api_request_arguments
        self.api_key = api_key

    def __iter__(self):
        return self

    def __aiter__(self):
        return self

    async def __aenter__(self):
        api_request_arguments = self.api_request_arugments
        if hasattr(self.generator, "_AsyncMessageStreamManager__api_request"):
            return GeneratorProxy(
                await self.generator._AsyncMessageStreamManager__api_request,
                api_request_arguments,
                self.api_key,
            )

    def __enter__(self):
        api_request_arguments = self.api_request_arugments
        if hasattr(self.generator, "_MessageStreamManager__api_request"):
            stream = self.generator.__enter__()
            return GeneratorProxy(
                stream,
                api_request_arguments,
                self.api_key,
            )

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def __anext__(self):
        result = await self.generator.__anext__()
        return self._abstracted_next(result)

    def __next__(self):
        result = next(self.generator)
        return self._abstracted_next(result)

    def __getattr__(self, name):
        if name == "text_stream":  # anthropic async stream
            return GeneratorProxy(
                self.generator.text_stream, self.api_request_arugments, self.api_key
            )
        return getattr(self.generator, name)

    def _abstracted_next(self, result):
        self.results.append(result)
        provider_type = self.api_request_arugments["provider_type"]
        end_anthropic = False

        if provider_type == "anthropic":
            if hasattr(result, "stop_reason"):
                end_anthropic = result.stop_reason
            elif hasattr(result, "message"):
                end_anthropic = result.message.stop_reason
            elif hasattr(result, "type") and result.type == "message_stop":
                end_anthropic = True

        end_openai = provider_type == "openai" and (
            result.choices[0].finish_reason == "stop"
            or result.choices[0].finish_reason == "length"
        )

        if end_anthropic or end_openai:
            request_id = promptlayer_api_request(
                function_name=self.api_request_arugments["function_name"],
                provider_type=self.api_request_arugments["provider_type"],
                args=self.api_request_arugments["args"],
                kwargs=self.api_request_arugments["kwargs"],
                tags=self.api_request_arugments["tags"],
                response=self.cleaned_result(),
                request_start_time=self.api_request_arugments["request_start_time"],
                request_end_time=self.api_request_arugments["request_end_time"],
                api_key=self.api_key,
                return_pl_id=self.api_request_arugments["return_pl_id"],
                llm_request_span_id=self.api_request_arugments.get(
                    "llm_request_span_id"
                ),
            )

            if self.api_request_arugments["return_pl_id"]:
                return result, request_id

        if self.api_request_arugments["return_pl_id"]:
            return result, None

        return result

    def cleaned_result(self):
        provider_type = self.api_request_arugments["provider_type"]
        if provider_type == "anthropic":
            response = ""
            for result in self.results:
                if hasattr(result, "completion"):
                    response = f"{response}{result.completion}"
                elif hasattr(result, "message") and isinstance(result.message, str):
                    response = f"{response}{result.message}"
                elif (
                    hasattr(result, "content_block")
                    and hasattr(result.content_block, "text")
                    and "type" in result
                    and result.type != "message_stop"
                ):
                    response = f"{response}{result.content_block.text}"
                elif hasattr(result, "delta") and hasattr(result.delta, "text"):
                    response = f"{response}{result.delta.text}"
            if (
                hasattr(self.results[-1], "type")
                and self.results[-1].type == "message_stop"
            ):  # this is a message stream and not the correct event
                final_result = deepcopy(self.results[0].message)
                final_result.usage = None
                content_block = deepcopy(self.results[1].content_block)
                content_block.text = response
                final_result.content = [content_block]
            else:
                final_result = deepcopy(self.results[-1])
                final_result.completion = response
            return final_result
        if hasattr(self.results[0].choices[0], "text"):  # this is regular completion
            response = ""
            for result in self.results:
                response = f"{response}{result.choices[0].text}"
            final_result = deepcopy(self.results[-1])
            final_result.choices[0].text = response
            return final_result
        elif hasattr(
            self.results[0].choices[0], "delta"
        ):  # this is completion with delta
            response = {"role": "", "content": ""}
            for result in self.results:
                if (
                    hasattr(result.choices[0].delta, "role")
                    and result.choices[0].delta.role is not None
                ):
                    response["role"] = result.choices[0].delta.role
                if (
                    hasattr(result.choices[0].delta, "content")
                    and result.choices[0].delta.content is not None
                ):
                    response["content"] = response[
                        "content"
                    ] = f"{response['content']}{result.choices[0].delta.content}"
            final_result = deepcopy(self.results[-1])
            final_result.choices[0] = response
            return final_result
        return ""


async def run_in_thread_async(executor, func, *args, **kwargs):
    """https://github.com/python/cpython/blob/main/Lib/asyncio/threads.py"""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    func_call = functools.partial(ctx.run, func, *args, **kwargs)
    res = await loop.run_in_executor(executor, func_call)
    return res


def warn_on_bad_response(request_response, main_message):
    if hasattr(request_response, "json"):
        try:
            print(
                f"{main_message}: {request_response.json().get('message')}",
                file=sys.stderr,
            )
        except json.JSONDecodeError:
            print(
                f"{main_message}: {request_response}",
                file=sys.stderr,
            )
    else:
        print(f"{main_message}: {request_response}", file=sys.stderr)


def raise_on_bad_response(request_response, main_message):
    if hasattr(request_response, "json"):
        try:
            raise Exception(f"{main_message}: {request_response.json().get('message')}")
        except json.JSONDecodeError:
            raise Exception(f"{main_message}: {request_response}")
    else:
        raise Exception(f"{main_message}: {request_response}")


async def async_wrapper(
    coroutine_obj,
    return_pl_id,
    request_start_time,
    function_name,
    provider_type,
    tags,
    api_key: str = None,
    llm_request_span_id: str = None,
    tracer=None,
    *args,
    **kwargs,
):
    current_context = context.get_current()
    token = context.attach(current_context)

    try:
        response = await coroutine_obj
        request_end_time = datetime.datetime.now().timestamp()
        result = await promptlayer_api_handler_async(
            function_name,
            provider_type,
            args,
            kwargs,
            tags,
            response,
            request_start_time,
            request_end_time,
            api_key,
            return_pl_id=return_pl_id,
            llm_request_span_id=llm_request_span_id,
        )

        if tracer:
            current_span = trace.get_current_span()
            if current_span:
                current_span.set_attribute("function_output", str(result))

        return result
    finally:
        context.detach(token)


def promptlayer_create_group(api_key: str = None):
    try:
        request_response = requests.post(
            f"{URL_API_PROMPTLAYER}/create-group",
            json={
                "api_key": api_key,
            },
        )
        if request_response.status_code != 200:
            warn_on_bad_response(
                request_response,
                "WARNING: While creating your group PromptLayer had the following error",
            )
            return False
    except requests.exceptions.RequestException as e:
        # I'm aiming for a more specific exception catch here
        raise Exception(
            f"PromptLayer had the following error while creating your group: {e}"
        )
    return request_response.json()["id"]


def promptlayer_track_group(request_id, group_id, api_key: str = None):
    try:
        request_response = requests.post(
            f"{URL_API_PROMPTLAYER}/track-group",
            json={
                "api_key": api_key,
                "request_id": request_id,
                "group_id": group_id,
            },
        )
        if request_response.status_code != 200:
            warn_on_bad_response(
                request_response,
                "WARNING: While tracking your group PromptLayer had the following error",
            )
            return False
    except requests.exceptions.RequestException as e:
        # I'm aiming for a more specific exception catch here
        raise Exception(
            f"PromptLayer had the following error while tracking your group: {e}"
        )
    return True


def get_prompt_template(
    prompt_name: str, params: Union[GetPromptTemplate, None] = None, api_key: str = None
) -> GetPromptTemplateResponse:
    try:
        json_body = {"api_key": api_key}
        if params:
            json_body = {**json_body, **params}
        response = requests.post(
            f"{URL_API_PROMPTLAYER}/prompt-templates/{prompt_name}",
            headers={"X-API-KEY": api_key},
            json=json_body,
        )
        if response.status_code != 200:
            raise Exception(
                f"PromptLayer had the following error while getting your prompt template: {response.text}"
            )
        return response.json()
    except requests.exceptions.RequestException as e:
        raise Exception(
            f"PromptLayer had the following error while getting your prompt template: {e}"
        )


def publish_prompt_template(
    body: PublishPromptTemplate,
    api_key: str = None,
) -> PublishPromptTemplateResponse:
    try:
        response = requests.post(
            f"{URL_API_PROMPTLAYER}/rest/prompt-templates",
            headers={"X-API-KEY": api_key},
            json={
                "prompt_template": {**body},
                "prompt_version": {**body},
                "release_labels": body.get("release_labels"),
            },
        )
        if response.status_code == 400:
            raise Exception(
                f"PromptLayer had the following error while publishing your prompt template: {response.text}"
            )
        return response.json()
    except requests.exceptions.RequestException as e:
        raise Exception(
            f"PromptLayer had the following error while publishing your prompt template: {e}"
        )


def get_all_prompt_templates(
    page: int = 1, per_page: int = 30, api_key: str = None
) -> List[ListPromptTemplateResponse]:
    try:
        response = requests.get(
            f"{URL_API_PROMPTLAYER}/prompt-templates",
            headers={"X-API-KEY": api_key},
            params={"page": page, "per_page": per_page},
        )
        if response.status_code != 200:
            raise Exception(
                f"PromptLayer had the following error while getting all your prompt templates: {response.text}"
            )
        items = response.json().get("items", [])
        return items
    except requests.exceptions.RequestException as e:
        raise Exception(
            f"PromptLayer had the following error while getting all your prompt templates: {e}"
        )


def openai_stream_chat(results: list):
    from openai.types.chat import (
        ChatCompletion,
        ChatCompletionChunk,
        ChatCompletionMessage,
    )
    from openai.types.chat.chat_completion import Choice

    chat_completion_chunks: List[ChatCompletionChunk] = results
    response: ChatCompletion = ChatCompletion(
        id="",
        object="chat.completion",
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(role="assistant"),
            )
        ],
        created=0,
        model="",
    )
    last_result = chat_completion_chunks[-1]
    response.id = last_result.id
    response.created = last_result.created
    response.model = last_result.model
    response.system_fingerprint = last_result.system_fingerprint
    response.usage = last_result.usage
    content = ""
    for result in chat_completion_chunks:
        if len(result.choices) > 0 and result.choices[0].delta.content:
            content = f"{content}{result.choices[0].delta.content}"
    response.choices[0].message.content = content
    return response


def openai_stream_completion(results: list):
    from openai.types.completion import Completion, CompletionChoice

    completions: List[Completion] = results
    last_chunk = completions[-1]
    response = Completion(
        id=last_chunk.id,
        created=last_chunk.created,
        model=last_chunk.model,
        object="text_completion",
        choices=[CompletionChoice(finish_reason="stop", index=0, text="")],
    )
    text = ""
    for completion in completions:
        usage = completion.usage
        system_fingerprint = completion.system_fingerprint
        if len(completion.choices) > 0 and completion.choices[0].text:
            text = f"{text}{completion.choices[0].text}"
        if usage:
            response.usage = usage
        if system_fingerprint:
            response.system_fingerprint = system_fingerprint
    response.choices[0].text = text
    return response


def anthropic_stream_message(results: list):
    from anthropic.types import Message, MessageStreamEvent, TextBlock, Usage

    message_stream_events: List[MessageStreamEvent] = results
    response: Message = Message(
        id="",
        model="",
        content=[],
        role="assistant",
        type="message",
        stop_reason="stop_sequence",
        stop_sequence=None,
        usage=Usage(input_tokens=0, output_tokens=0),
    )
    content = ""
    for result in message_stream_events:
        if result.type == "message_start":
            response = result.message
        elif result.type == "content_block_delta":
            if result.delta.type == "text_delta":
                content = f"{content}{result.delta.text}"
        elif result.type == "message_delta":
            if hasattr(result, "usage"):
                response.usage.output_tokens = result.usage.output_tokens
            if hasattr(result.delta, "stop_reason"):
                response.stop_reason = result.delta.stop_reason
    response.content.append(TextBlock(type="text", text=content))
    return response


def anthropic_stream_completion(results: list):
    from anthropic.types import Completion

    completions: List[Completion] = results
    last_chunk = completions[-1]
    response = Completion(
        id=last_chunk.id,
        completion="",
        model=last_chunk.model,
        stop_reason="stop",
        type="completion",
    )

    text = ""
    for completion in completions:
        text = f"{text}{completion.completion}"
    response.completion = text
    return response


def stream_response(
    generator: Generator, after_stream: Callable, map_results: Callable
):
    data = {
        "request_id": None,
        "raw_response": None,
        "prompt_blueprint": None,
    }
    results = []
    for result in generator:
        results.append(result)
        data["raw_response"] = result
        yield data
    request_response = map_results(results)
    response = after_stream(request_response=request_response.model_dump())
    data["request_id"] = response.get("request_id")
    data["prompt_blueprint"] = response.get("prompt_blueprint")
    yield data


def openai_chat_request(client, **kwargs):
    return client.chat.completions.create(**kwargs)


def openai_completions_request(client, **kwargs):
    return client.completions.create(**kwargs)


MAP_TYPE_TO_OPENAI_FUNCTION = {
    "chat": openai_chat_request,
    "completion": openai_completions_request,
}


def openai_request(prompt_blueprint: GetPromptTemplateResponse, **kwargs):
    from openai import OpenAI

    client = OpenAI(base_url=kwargs.pop("base_url", None))
    request_to_make = MAP_TYPE_TO_OPENAI_FUNCTION[
        prompt_blueprint["prompt_template"]["type"]
    ]
    return request_to_make(client, **kwargs)


def anthropic_chat_request(client, **kwargs):
    return client.messages.create(**kwargs)


def anthropic_completions_request(client, **kwargs):
    return client.completions.create(**kwargs)


MAP_TYPE_TO_ANTHROPIC_FUNCTION = {
    "chat": anthropic_chat_request,
    "completion": anthropic_completions_request,
}


def anthropic_request(prompt_blueprint: GetPromptTemplateResponse, **kwargs):
    from anthropic import Anthropic

    client = Anthropic(base_url=kwargs.pop("base_url", None))
    request_to_make = MAP_TYPE_TO_ANTHROPIC_FUNCTION[
        prompt_blueprint["prompt_template"]["type"]
    ]
    return request_to_make(client, **kwargs)


# do not remove! This is used in the langchain integration.
def get_api_key():
    # raise an error if the api key is not set
    api_key = os.environ.get("PROMPTLAYER_API_KEY")
    if not api_key:
        raise Exception(
            "Please set your PROMPTLAYER_API_KEY environment variable or set API KEY in code using 'promptlayer.api_key = <your_api_key>' "
        )
    return api_key
