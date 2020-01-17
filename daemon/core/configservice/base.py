import abc
import enum
import inspect
import logging
import pathlib
import time
from typing import Any, Dict, List

from mako import exceptions
from mako.lookup import TemplateLookup

from core.errors import CoreCommandError, CoreError
from core.nodes.base import CoreNode

TEMPLATES_DIR = "templates"


class ConfigServiceMode(enum.Enum):
    BLOCKING = 0
    NON_BLOCKING = 1
    TIMER = 2


class ConfigService(abc.ABC):
    # validation period in seconds, how frequent validation is attempted
    validation_period = 0.5

    # time to wait in seconds for determining if service started successfully
    validation_timer = 5

    def __init__(self, node: CoreNode) -> None:
        self.node = node
        class_file = inspect.getfile(self.__class__)
        templates_path = pathlib.Path(class_file).parent.joinpath(TEMPLATES_DIR)
        logging.info(templates_path)
        self.templates = TemplateLookup(directories=templates_path)

    @property
    @abc.abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def group(self) -> str:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def directories(self) -> List[str]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def executables(self) -> List[str]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def dependencies(self) -> List[str]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def startup(self) -> List[str]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def validate(self) -> List[str]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def shutdown(self) -> List[str]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def validation_mode(self) -> ConfigServiceMode:
        raise NotImplementedError

    def start(self) -> None:
        self.create_dirs()
        self.create_files()
        self.run_startup()
        self.run_validation()

    def stop(self) -> None:
        for cmd in self.shutdown:
            try:
                self.node.cmd(cmd)
            except CoreCommandError:
                logging.exception(
                    f"node({self.node.name}) service({self.name}) "
                    f"failed shutdown: {cmd}"
                )

    def restart(self) -> None:
        self.stop()
        self.start()

    def create_dirs(self) -> None:
        for directory in self.directories:
            try:
                self.node.privatedir(directory)
            except (CoreCommandError, ValueError):
                raise CoreError(
                    f"node({self.node.name}) service({self.name}) "
                    f"failure to create service directory: {directory}"
                )

    def create_files(self) -> None:
        raise NotImplementedError

    def run_startup(self) -> None:
        for cmd in self.startup:
            try:
                self.node.cmd(cmd)
            except CoreCommandError:
                raise CoreError(
                    f"node({self.node.name}) service({self.name}) "
                    f"failed startup: {cmd}"
                )

    def run_validation(self) -> None:
        wait = self.validation_mode == ConfigServiceMode.BLOCKING
        start = time.monotonic()
        index = 0
        cmds = self.startup[:]
        while cmds:
            cmd = cmds[index]
            try:
                self.node.cmd(cmd, wait=wait)
                del cmds[index]
                index += 1
            except CoreCommandError:
                logging.debug(
                    f"node({self.node.name}) service({self.name}) "
                    f"validate command failed: {cmd}"
                )
                time.sleep(self.validation_period)

            if time.monotonic() - start > 0:
                raise CoreError(
                    f"node({self.node.name}) service({self.name}) "
                    f"failed to validate"
                )

    def render(self, name: str, data: Dict[str, Any] = None) -> None:
        if data is None:
            data = {}
        try:
            template = self.templates.get_template(name)
            rendered = template.render_unicode(node=self.node, **data)
            logging.info(
                "node(%s) service(%s) template(%s): \n%s",
                self.node.name,
                self.name,
                name,
                rendered,
            )
            # self.node.nodefile(name, rendered)
        except Exception:
            raise CoreError(
                f"node({self.node.name}) service({self.name}) "
                f"error rendering template({name}): "
                f"{exceptions.text_error_template().render()}"
            )
