import logging
import tkinter as tk
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image
from PIL.Image import Resampling
from PIL.ImageTk import PhotoImage

from core.api.grpc.wrappers import Interface, Link
from core.gui import appconfig
from core.gui import nodeutils as nutils
from core.gui.dialogs.shapemod import ShapeDialog
from core.gui.graph import tags
from core.gui.graph.edges import EDGE_WIDTH, CanvasEdge
from core.gui.graph.enums import GraphMode, ScaleOption
from core.gui.graph.node import CanvasNode, ShadowNode
from core.gui.graph.shape import Shape
from core.gui.graph.shapeutils import ShapeType, is_draw_shape, is_marker

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.gui.app import Application
    from core.gui.coreclient import CoreClient
    from core.gui.graph.manager import CanvasManager

ZOOM_IN: float = 1.1
ZOOM_OUT: float = 0.9
MOVE_NODE_MODES: set[GraphMode] = {GraphMode.NODE, GraphMode.SELECT}
MOVE_SHAPE_MODES: set[GraphMode] = {GraphMode.ANNOTATION, GraphMode.SELECT}
BACKGROUND_COLOR: str = "#cccccc"


class CanvasGraph(tk.Canvas):
    def __init__(
        self,
        master: tk.BaseWidget,
        app: "Application",
        manager: "CanvasManager",
        core: "CoreClient",
        _id: int,
        dimensions: tuple[int, int],
    ) -> None:
        super().__init__(master, highlightthickness=0, background=BACKGROUND_COLOR)
        self.id: int = _id
        self.app: "Application" = app
        self.manager: "CanvasManager" = manager
        self.core: "CoreClient" = core
        self.selection: dict[int, int] = {}
        self.select_box: Shape | None = None
        self.selected: int | None = None
        self.nodes: dict[int, CanvasNode] = {}
        self.shadow_nodes: dict[int, ShadowNode] = {}
        self.shapes: dict[int, Shape] = {}
        self.shadow_core_nodes: dict[int, ShadowNode] = {}

        # map wireless/EMANE node to the set of MDRs connected to that node
        self.wireless_network: dict[int, set[int]] = {}

        self.drawing_edge: CanvasEdge | None = None
        self.rect: int | None = None
        self.shape_drawing: bool = False
        self.current_dimensions: tuple[int, int] = dimensions
        self.ratio: float = 1.0
        self.offset: tuple[int, int] = (0, 0)
        self.cursor: tuple[int, int] = (0, 0)
        self.to_copy: list[CanvasNode] = []

        # background related
        self.wallpaper_id: int | None = None
        self.wallpaper: Image.Image | None = None
        self.wallpaper_drawn: PhotoImage | None = None
        self.wallpaper_file: str = ""
        self.scale_option: tk.IntVar = tk.IntVar(value=1)
        self.adjust_to_dim: tk.BooleanVar = tk.BooleanVar(value=False)

        # bindings
        self.setup_bindings()

        # draw base canvas
        self.draw_canvas()
        self.draw_grid()

    def draw_canvas(self, dimensions: tuple[int, int] = None) -> None:
        if self.rect is not None:
            self.delete(self.rect)
        if not dimensions:
            dimensions = self.manager.default_dimensions
        self.current_dimensions = dimensions
        self.rect = self.create_rectangle(
            0,
            0,
            *dimensions,
            outline="#000000",
            fill="#ffffff",
            width=1,
            tags="rectangle",
        )
        self.configure(scrollregion=self.bbox(tk.ALL))

    def setup_bindings(self) -> None:
        """
        Bind any mouse events or hot keys to the matching action
        """
        self.bind("<Control-c>", self.copy_selected)
        self.bind("<Control-v>", self.paste_selected)
        self.bind("<Control-x>", self.cut_selected)
        self.bind("<Control-d>", self.delete_selected)
        self.bind("<Control-h>", self.hide_selected)
        self.bind("<ButtonPress-1>", self.click_press)
        self.bind("<ButtonRelease-1>", self.click_release)
        self.bind("<B1-Motion>", self.click_motion)
        self.bind("<Delete>", self.delete_selected)
        self.bind("<Control-1>", self.ctrl_click)
        self.bind("<Double-Button-1>", self.double_click)
        self.bind("<MouseWheel>", self.zoom)
        self.bind("<Button-4>", lambda e: self.zoom(e, ZOOM_IN))
        self.bind("<Button-5>", lambda e: self.zoom(e, ZOOM_OUT))
        self.bind("<ButtonPress-3>", lambda e: self.scan_mark(e.x, e.y))
        self.bind("<B3-Motion>", lambda e: self.scan_dragto(e.x, e.y, gain=1))

    def get_shadow(self, node: CanvasNode) -> ShadowNode:
        shadow_node = self.shadow_core_nodes.get(node.core_node.id)
        if not shadow_node:
            shadow_node = ShadowNode(self.app, self, node)
        return shadow_node

    def get_actual_coords(self, x: float, y: float) -> tuple[float, float]:
        actual_x = (x - self.offset[0]) / self.ratio
        actual_y = (y - self.offset[1]) / self.ratio
        return actual_x, actual_y

    def get_scaled_coords(self, x: float, y: float) -> tuple[float, float]:
        scaled_x = (x * self.ratio) + self.offset[0]
        scaled_y = (y * self.ratio) + self.offset[1]
        return scaled_x, scaled_y

    def inside_canvas(self, x: float, y: float) -> tuple[bool, bool]:
        x1, y1, x2, y2 = self.bbox(self.rect)
        valid_x = x1 <= x <= x2
        valid_y = y1 <= y <= y2
        return valid_x and valid_y

    def valid_position(self, x1: int, y1: int, x2: int, y2: int) -> tuple[bool, bool]:
        valid_topleft = self.inside_canvas(x1, y1)
        valid_bottomright = self.inside_canvas(x2, y2)
        return valid_topleft and valid_bottomright

    def draw_grid(self) -> None:
        """
        Create grid.
        """
        width, height = self.width_and_height()
        width = int(width)
        height = int(height)
        for i in range(0, width, 27):
            self.create_line(i, 0, i, height, dash=(2, 4), tags=tags.GRIDLINE)
        for i in range(0, height, 27):
            self.create_line(0, i, width, i, dash=(2, 4), tags=tags.GRIDLINE)
        self.tag_lower(tags.GRIDLINE)
        self.tag_lower(self.rect)

    def canvas_xy(self, event: tk.Event) -> tuple[float, float]:
        """
        Convert window coordinate to canvas coordinate
        """
        x = self.canvasx(event.x)
        y = self.canvasy(event.y)
        return x, y

    def get_selected(self, event: tk.Event) -> int:
        """
        Retrieve the item id that is on the mouse position
        """
        x, y = self.canvas_xy(event)
        overlapping = self.find_overlapping(x, y, x, y)
        selected = None
        for _id in overlapping:
            if self.drawing_edge and self.drawing_edge.id == _id:
                continue
            elif _id in self.nodes:
                selected = _id
            elif _id in self.shapes:
                selected = _id
            elif _id in self.shadow_nodes:
                selected = _id
        return selected

    def click_release(self, event: tk.Event) -> None:
        """
        Draw a node or finish drawing an edge according to the current graph mode
        """
        logger.debug("click release")
        x, y = self.canvas_xy(event)
        if not self.inside_canvas(x, y):
            return
        if self.manager.mode == GraphMode.ANNOTATION:
            self.focus_set()
            if self.shape_drawing:
                shape = self.shapes[self.selected]
                shape.shape_complete(x, y)
                self.shape_drawing = False
        elif self.manager.mode == GraphMode.SELECT:
            self.focus_set()
            if self.select_box:
                x0, y0, x1, y1 = self.coords(self.select_box.id)
                inside = [
                    x
                    for x in self.find_enclosed(x0, y0, x1, y1)
                    if "node" in self.gettags(x) or "shape" in self.gettags(x)
                ]
                for i in inside:
                    self.select_object(i, True)
                self.select_box.disappear()
                self.select_box = None
        else:
            self.focus_set()
            self.selected = self.get_selected(event)
            logger.debug(
                "click release selected(%s) mode(%s)", self.selected, self.manager.mode
            )
            if self.manager.mode == GraphMode.EDGE:
                self.handle_edge_release(event)
            elif self.manager.mode == GraphMode.NODE:
                self.add_node(x, y)
            elif self.manager.mode == GraphMode.PICKNODE:
                self.manager.mode = GraphMode.NODE
        self.selected = None

    def handle_edge_release(self, _event: tk.Event) -> None:
        # not drawing edge return
        if not self.drawing_edge:
            return
        edge = self.drawing_edge
        self.drawing_edge = None
        # edge dst must be a node
        logger.debug("current selected: %s", self.selected)
        dst_node = self.nodes.get(self.selected)
        if not dst_node:
            edge.delete()
            return
        # check if node can be linked
        if not edge.src.is_linkable(dst_node):
            edge.delete()
            return
        # finalize edge creation
        edge.drawing(dst_node.position())
        edge.complete(dst_node)

    def select_object(self, object_id: int, choose_multiple: bool = False) -> None:
        """
        create a bounding box when a node is selected
        """
        if not choose_multiple:
            self.clear_selection()

        # draw a bounding box if node hasn't been selected yet
        if object_id not in self.selection:
            x0, y0, x1, y1 = self.bbox(object_id)
            selection_id = self.create_rectangle(
                (x0 - 6, y0 - 6, x1 + 6, y1 + 6),
                activedash=True,
                dash="-",
                tags=tags.SELECTION,
            )
            self.selection[object_id] = selection_id
        else:
            selection_id = self.selection.pop(object_id)
            self.delete(selection_id)

    def clear_selection(self) -> None:
        """
        Clear current selection boxes.
        """
        for _id in self.selection.values():
            self.delete(_id)
        self.selection.clear()

    def move_selection(self, object_id: int, x_offset: float, y_offset: float) -> None:
        select_id = self.selection.get(object_id)
        if select_id is not None:
            self.move(select_id, x_offset, y_offset)

    def delete_selected_objects(self, _event: tk.Event = None) -> None:
        edges = set()
        nodes = []
        for object_id in self.selection:
            #  delete selection box
            selection_id = self.selection[object_id]
            self.delete(selection_id)

            # delete node and related edges
            if object_id in self.nodes:
                canvas_node = self.nodes.pop(object_id)
                # delete related edges
                while canvas_node.edges:
                    edge = canvas_node.edges.pop()
                    if edge in edges:
                        continue
                    edges.add(edge)
                    edge.delete()
                # delete node
                canvas_node.delete()
                nodes.append(canvas_node)

            # delete shape
            if object_id in self.shapes:
                shape = self.shapes.pop(object_id)
                shape.delete()

        self.selection.clear()
        self.core.deleted_canvas_nodes(nodes)

    def hide_selected(self, _event: tk.Event = None) -> None:
        for object_id in self.selection:
            #  delete selection box
            selection_id = self.selection[object_id]
            self.delete(selection_id)
            # hide node and related edges
            if object_id in self.nodes:
                canvas_node = self.nodes[object_id]
                canvas_node.hide()

    def show_hidden(self) -> None:
        for node in self.nodes.values():
            if node.hidden:
                node.show()

    def zoom(self, event: tk.Event, factor: float = None) -> None:
        if not factor:
            factor = ZOOM_IN if event.delta > 0 else ZOOM_OUT
        event.x, event.y = self.canvasx(event.x), self.canvasy(event.y)
        self.scale(tk.ALL, event.x, event.y, factor, factor)
        self.configure(scrollregion=self.bbox(tk.ALL))
        self.ratio *= float(factor)
        self.offset = (
            self.offset[0] * factor + event.x * (1 - factor),
            self.offset[1] * factor + event.y * (1 - factor),
        )
        logger.debug("ratio: %s", self.ratio)
        logger.debug("offset: %s", self.offset)
        self.app.statusbar.set_zoom(self.ratio)
        if self.wallpaper:
            self.redraw_wallpaper()

    def click_press(self, event: tk.Event) -> None:
        """
        Start drawing an edge if mouse click is on a node
        """
        x, y = self.canvas_xy(event)
        if not self.inside_canvas(x, y):
            return

        self.cursor = x, y
        selected = self.get_selected(event)
        logger.debug("click press(%s): %s", self.cursor, selected)
        x_check = self.cursor[0] - self.offset[0]
        y_check = self.cursor[1] - self.offset[1]
        logger.debug("click press offset(%s, %s)", x_check, y_check)
        is_node = selected in self.nodes
        if self.manager.mode == GraphMode.EDGE and is_node:
            node = self.nodes[selected]
            self.drawing_edge = CanvasEdge(self.app, node)
            self.organize()

        if self.manager.mode == GraphMode.ANNOTATION:
            if is_marker(self.manager.annotation_type):
                r = self.app.toolbar.marker_frame.size.get()
                self.create_oval(
                    x - r,
                    y - r,
                    x + r,
                    y + r,
                    fill=self.app.toolbar.marker_frame.color,
                    outline="",
                    tags=(tags.MARKER, tags.ANNOTATION),
                    state=self.manager.show_annotations.state(),
                )
                return
            if selected is None:
                shape = Shape(self.app, self, self.manager.annotation_type, x, y)
                self.selected = shape.id
                self.shape_drawing = True
                self.shapes[shape.id] = shape

        if selected is not None:
            if selected not in self.selection:
                if selected in self.shapes:
                    shape = self.shapes[selected]
                    self.select_object(shape.id)
                    self.selected = selected
                elif selected in self.nodes:
                    node = self.nodes[selected]
                    self.select_object(node.id)
                    self.selected = selected
                    logger.debug(
                        "selected node(%s), coords: (%s, %s)",
                        node.core_node.name,
                        node.core_node.position.x,
                        node.core_node.position.y,
                    )
                elif selected in self.shadow_nodes:
                    shadow_node = self.shadow_nodes[selected]
                    self.select_object(shadow_node.id)
                    self.selected = selected
                    logger.debug(
                        "selected shadow node(%s), coords: (%s, %s)",
                        shadow_node.node.core_node.name,
                        shadow_node.node.core_node.position.x,
                        shadow_node.node.core_node.position.y,
                    )
        else:
            if self.manager.mode == GraphMode.SELECT:
                shape = Shape(self.app, self, ShapeType.RECTANGLE, x, y)
                self.select_box = shape
            self.clear_selection()

    def ctrl_click(self, event: tk.Event) -> None:
        # update cursor location
        x, y = self.canvas_xy(event)
        if not self.inside_canvas(x, y):
            return

        self.cursor = x, y

        # handle multiple selections
        logger.debug("control left click: %s", event)
        selected = self.get_selected(event)
        if (
            selected not in self.selection
            and selected in self.shapes
            or selected in self.nodes
        ):
            self.select_object(selected, choose_multiple=True)

    def click_motion(self, event: tk.Event) -> None:
        x, y = self.canvas_xy(event)
        if not self.inside_canvas(x, y):
            if self.select_box:
                self.select_box.delete()
                self.select_box = None
            if is_draw_shape(self.manager.annotation_type) and self.shape_drawing:
                shape = self.shapes.pop(self.selected)
                shape.delete()
                self.shape_drawing = False
            return

        x_offset = x - self.cursor[0]
        y_offset = y - self.cursor[1]
        self.cursor = x, y

        if self.manager.mode == GraphMode.EDGE and self.drawing_edge is not None:
            self.drawing_edge.drawing(self.cursor)
        if self.manager.mode == GraphMode.ANNOTATION:
            if is_draw_shape(self.manager.annotation_type) and self.shape_drawing:
                shape = self.shapes[self.selected]
                shape.shape_motion(x, y)
                return
            elif is_marker(self.manager.annotation_type):
                r = self.app.toolbar.marker_frame.size.get()
                self.create_oval(
                    x - r,
                    y - r,
                    x + r,
                    y + r,
                    fill=self.app.toolbar.marker_frame.color,
                    outline="",
                    tags=(tags.MARKER, tags.ANNOTATION),
                )
                return

        if self.manager.mode == GraphMode.EDGE:
            return

        # move selected objects
        if self.selection:
            for selected_id in self.selection:
                if self.manager.mode in MOVE_SHAPE_MODES and selected_id in self.shapes:
                    shape = self.shapes[selected_id]
                    shape.motion(x_offset, y_offset)
                elif self.manager.mode in MOVE_NODE_MODES and selected_id in self.nodes:
                    node = self.nodes[selected_id]
                    node.motion(x_offset, y_offset, update=self.core.is_runtime())
                elif (
                    self.manager.mode in MOVE_NODE_MODES
                    and selected_id in self.shadow_nodes
                ):
                    shadow_node = self.shadow_nodes[selected_id]
                    shadow_node.motion(x_offset, y_offset)
        else:
            if self.select_box and self.manager.mode == GraphMode.SELECT:
                self.select_box.shape_motion(x, y)

    def double_click(self, event: tk.Event) -> None:
        selected = self.get_selected(event)
        if selected is not None and selected in self.shapes:
            shape = self.shapes[selected]
            dialog = ShapeDialog(self.app, shape)
            dialog.show()

    def add_node(self, x: float, y: float) -> None:
        if self.selected is not None and self.selected not in self.shapes:
            return
        actual_x, actual_y = self.get_actual_coords(x, y)
        core_node = self.core.create_node(
            actual_x,
            actual_y,
            self.manager.node_draw.node_type,
            self.manager.node_draw.model,
        )
        if not core_node:
            return
        core_node.canvas = self.id
        node = CanvasNode(self.app, self, x, y, core_node, self.manager.node_draw.image)
        self.nodes[node.id] = node
        self.core.set_canvas_node(core_node, node)

    def width_and_height(self) -> tuple[int, int]:
        """
        retrieve canvas width and height in pixels
        """
        x0, y0, x1, y1 = self.coords(self.rect)
        canvas_w = abs(x0 - x1)
        canvas_h = abs(y0 - y1)
        return canvas_w, canvas_h

    def get_wallpaper_image(self) -> Image.Image:
        width = int(self.wallpaper.width * self.ratio)
        height = int(self.wallpaper.height * self.ratio)
        image = self.wallpaper.resize((width, height), Resampling.LANCZOS)
        return image

    def draw_wallpaper(
        self, image: PhotoImage, x: float = None, y: float = None
    ) -> None:
        if x is None and y is None:
            x1, y1, x2, y2 = self.bbox(self.rect)
            x = (x1 + x2) / 2
            y = (y1 + y2) / 2
        self.wallpaper_id = self.create_image((x, y), image=image, tags=tags.WALLPAPER)
        self.wallpaper_drawn = image

    def wallpaper_upper_left(self) -> None:
        self.delete(self.wallpaper_id)

        # create new scaled image, cropped if needed
        width, height = self.width_and_height()
        image = self.get_wallpaper_image()
        cropx = image.width
        cropy = image.height
        if image.width > width:
            cropx = image.width
        if image.height > height:
            cropy = image.height
        cropped = image.crop((0, 0, cropx, cropy))
        image = PhotoImage(cropped)

        # draw on canvas
        x1, y1, _, _ = self.bbox(self.rect)
        x = (cropx / 2) + x1
        y = (cropy / 2) + y1
        self.draw_wallpaper(image, x, y)

    def wallpaper_center(self) -> None:
        """
        place the image at the center of canvas
        """
        self.delete(self.wallpaper_id)

        # dimension of the cropped image
        width, height = self.width_and_height()
        image = self.get_wallpaper_image()
        cropx = 0
        if image.width > width:
            cropx = (image.width - width) / 2
        cropy = 0
        if image.height > height:
            cropy = (image.height - height) / 2
        x1 = 0 + cropx
        y1 = 0 + cropy
        x2 = image.width - cropx
        y2 = image.height - cropy
        cropped = image.crop((x1, y1, x2, y2))
        image = PhotoImage(cropped)
        self.draw_wallpaper(image)

    def wallpaper_scaled(self) -> None:
        """
        scale image based on canvas dimension
        """
        self.delete(self.wallpaper_id)
        canvas_w, canvas_h = self.width_and_height()
        image = self.wallpaper.resize(
            (int(canvas_w), int(canvas_h)), Resampling.LANCZOS
        )
        image = PhotoImage(image)
        self.draw_wallpaper(image)

    def resize_to_wallpaper(self) -> None:
        self.delete(self.wallpaper_id)
        image = PhotoImage(self.wallpaper)
        self.redraw_canvas((image.width(), image.height()))
        self.draw_wallpaper(image)

    def redraw_canvas(self, dimensions: tuple[int, int] = None) -> None:
        logger.debug("redrawing canvas to dimensions: %s", dimensions)

        # reset scale and move back to original position
        logger.debug("resetting scaling: %s %s", self.ratio, self.offset)
        factor = 1 / self.ratio
        self.scale(tk.ALL, self.offset[0], self.offset[1], factor, factor)
        self.move(tk.ALL, -self.offset[0], -self.offset[1])

        # reset ratio and offset
        self.ratio = 1.0
        self.offset = (0, 0)

        # redraw canvas rectangle
        self.draw_canvas(dimensions)

        # redraw gridlines to new canvas size
        self.delete(tags.GRIDLINE)
        self.draw_grid()
        self.app.manager.show_grid.click_handler()

    def redraw_wallpaper(self) -> None:
        if self.adjust_to_dim.get():
            logger.debug("drawing wallpaper to canvas dimensions")
            self.resize_to_wallpaper()
        else:
            option = ScaleOption(self.scale_option.get())
            logger.debug("drawing canvas using scaling option: %s", option)
            if option == ScaleOption.UPPER_LEFT:
                self.wallpaper_upper_left()
            elif option == ScaleOption.CENTERED:
                self.wallpaper_center()
            elif option == ScaleOption.SCALED:
                self.wallpaper_scaled()
            elif option == ScaleOption.TILED:
                logger.warning("tiled background not implemented yet")
        self.organize()

    def organize(self) -> None:
        for tag in tags.ORGANIZE_TAGS:
            self.tag_raise(tag)

    def set_wallpaper(self, filename: str | None) -> None:
        logger.info("setting canvas(%s) background: %s", self.id, filename)
        if filename:
            img = Image.open(filename)
            self.wallpaper = img
            self.wallpaper_file = filename
            self.redraw_wallpaper()
        else:
            if self.wallpaper_id is not None:
                self.delete(self.wallpaper_id)
            self.wallpaper = None
            self.wallpaper_file = None

    def is_selection_mode(self) -> bool:
        return self.manager.mode == GraphMode.SELECT

    def create_edge(self, src: CanvasNode, dst: CanvasNode) -> CanvasEdge:
        """
        create an edge between source node and destination node
        """
        edge = CanvasEdge(self.app, src)
        edge.complete(dst)
        return edge

    def copy_selected(self, _event: tk.Event = None) -> None:
        if self.core.is_runtime():
            logger.debug("copy is disabled during runtime state")
            return
        if self.selection:
            logger.debug("to copy nodes: %s", self.selection)
            self.to_copy.clear()
            for node_id in self.selection.keys():
                canvas_node = self.nodes[node_id]
                self.to_copy.append(canvas_node)

    def cut_selected(self, _event: tk.Event = None) -> None:
        if self.core.is_runtime():
            logger.debug("cut is disabled during runtime state")
            return
        self.copy_selected()
        self.delete_selected()

    def delete_selected(self, _event: tk.Event = None) -> None:
        """
        delete selected nodes and any data that relates to it
        """
        logger.debug("press delete key")
        if self.core.is_runtime():
            logger.debug("node deletion is disabled during runtime state")
            return
        self.delete_selected_objects()
        self.app.default_info()

    def paste_selected(self, _event: tk.Event = None) -> None:
        if self.core.is_runtime():
            logger.debug("paste is disabled during runtime state")
            return
        # maps original node canvas id to copy node canvas id
        copy_map = {}
        # the edges that will be copy over
        to_copy_edges = set()
        to_copy_ids = {x.id for x in self.to_copy}
        for canvas_node in self.to_copy:
            core_node = canvas_node.core_node
            actual_x = core_node.position.x + 50
            actual_y = core_node.position.y + 50
            scaled_x, scaled_y = self.get_scaled_coords(actual_x, actual_y)
            copy = self.core.create_node(
                actual_x, actual_y, core_node.type, core_node.model
            )
            if not copy:
                continue
            node = CanvasNode(
                self.app, self, scaled_x, scaled_y, copy, canvas_node.image
            )
            # copy configurations and services
            node.core_node.services = core_node.services.copy()
            node.core_node.emane_model_configs = deepcopy(core_node.emane_model_configs)
            node.core_node.wlan_config = deepcopy(core_node.wlan_config)
            node.core_node.mobility_config = deepcopy(core_node.mobility_config)
            node.core_node.service_configs = deepcopy(core_node.service_configs)
            node.core_node.image = core_node.image
            node.core_node.compose = core_node.compose

            copy_map[canvas_node.id] = node.id
            self.nodes[node.id] = node
            self.core.set_canvas_node(copy, node)
            for edge in canvas_node.edges:
                if edge.src not in to_copy_ids or edge.dst not in to_copy_ids:
                    if canvas_node.id == edge.src:
                        dst_node = self.nodes[edge.dst]
                        copy_edge = self.create_edge(node, dst_node)
                    elif canvas_node.id == edge.dst:
                        src_node = self.nodes[edge.src]
                        copy_edge = self.create_edge(src_node, node)
                    else:
                        continue
                    copy_link = copy_edge.link
                    iface1_id = copy_link.iface1.id if copy_link.iface1 else None
                    iface2_id = copy_link.iface2.id if copy_link.iface2 else None
                    options = edge.link.options
                    if options:
                        copy_edge.link.options = deepcopy(options)
                    if options and options.unidirectional:
                        asym_iface1 = None
                        if iface1_id is not None:
                            asym_iface1 = Interface(id=iface1_id)
                        asym_iface2 = None
                        if iface2_id is not None:
                            asym_iface2 = Interface(id=iface2_id)
                        copy_edge.asymmetric_link = Link(
                            node1_id=copy_link.node2_id,
                            node2_id=copy_link.node1_id,
                            iface1=asym_iface2,
                            iface2=asym_iface1,
                            options=deepcopy(edge.asymmetric_link.options),
                        )
                    copy_edge.redraw()
                else:
                    to_copy_edges.add(edge)

        # copy link and link config
        for edge in to_copy_edges:
            src_node_id = copy_map[edge.src]
            dst_node_id = copy_map[edge.dst]
            src_node_copy = self.nodes[src_node_id]
            dst_node_copy = self.nodes[dst_node_id]
            copy_edge = self.create_edge(src_node_copy, dst_node_copy)
            copy_link = copy_edge.link
            iface1_id = copy_link.iface1.id if copy_link.iface1 else None
            iface2_id = copy_link.iface2.id if copy_link.iface2 else None
            options = edge.link.options
            if options:
                copy_link.options = deepcopy(options)
            if options and options.unidirectional:
                asym_iface1 = None
                if iface1_id is not None:
                    asym_iface1 = Interface(id=iface1_id)
                asym_iface2 = None
                if iface2_id is not None:
                    asym_iface2 = Interface(id=iface2_id)
                copy_edge.asymmetric_link = Link(
                    node1_id=copy_link.node2_id,
                    node2_id=copy_link.node1_id,
                    iface1=asym_iface2,
                    iface2=asym_iface1,
                    options=deepcopy(edge.asymmetric_link.options),
                )
            copy_edge.redraw()
            self.itemconfig(
                copy_edge.id,
                width=self.itemcget(edge.id, "width"),
                fill=self.itemcget(edge.id, "fill"),
            )
        self.tag_raise(tags.NODE)

    def scale_graph(self) -> None:
        for node_id, canvas_node in self.nodes.items():
            image = nutils.get_icon(canvas_node.core_node, self.app)
            self.itemconfig(node_id, image=image)
            canvas_node.image = image
            canvas_node.scale_text()
            canvas_node.scale_antennas()
        for edge_id in self.find_withtag(tags.EDGE):
            self.itemconfig(edge_id, width=int(EDGE_WIDTH * self.app.app_scale))

    def get_metadata(self) -> dict[str, Any]:
        wallpaper_path = None
        if self.wallpaper_file:
            wallpaper = Path(self.wallpaper_file)
            if appconfig.BACKGROUNDS_PATH == wallpaper.parent:
                wallpaper_path = wallpaper.name
            else:
                wallpaper_path = str(wallpaper)
        return dict(
            id=self.id,
            wallpaper=wallpaper_path,
            wallpaper_style=self.scale_option.get(),
            fit_image=self.adjust_to_dim.get(),
            dimensions=self.current_dimensions,
        )

    def parse_metadata(self, config: dict[str, Any]) -> None:
        fit_image = config.get("fit_image", False)
        self.adjust_to_dim.set(fit_image)
        wallpaper_style = config.get("wallpaper_style", 1)
        self.scale_option.set(wallpaper_style)
        dimensions = config.get("dimensions")
        if dimensions:
            self.redraw_canvas(dimensions)
        wallpaper = config.get("wallpaper")
        if wallpaper:
            wallpaper = Path(wallpaper)
            if not wallpaper.is_file():
                wallpaper = appconfig.BACKGROUNDS_PATH.joinpath(wallpaper)
            logger.info("canvas(%s), wallpaper: %s", self.id, wallpaper)
            if wallpaper.is_file():
                self.set_wallpaper(str(wallpaper))
            else:
                self.app.show_error(
                    "Background Error", f"background file not found: {wallpaper}"
                )
