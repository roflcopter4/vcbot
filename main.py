import datetime
import logging
import os
import sys
import traceback
import typing
from typing import Any, Dict, List, Tuple

import base64
import zstd

import discord
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image


MAX_ZOOM = 24


class CustomBot(commands.Bot):
    _uptime: datetime.datetime = datetime.datetime.utcnow()

    def __init__(self, prefix: str, *args: Any, **kwargs: Any):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(*args, **kwargs, command_prefix=commands.when_mentioned_or(prefix), intents=intents)
        self.logger = logging.getLogger(self.__class__.__name__)

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        self.logger.error(f"An error occurred in {event_method}.\n{traceback.format_exc()}")

    async def on_ready(self) -> None:
        self.logger.info(f"Logged in as {self.user} ({self.user.id})")

    def run(self, *args: Any, **kwargs: Any) -> None:
        load_dotenv()
        try:
            super().run(str(os.getenv("TOKEN")), *args, **kwargs)
        except (discord.LoginFailure, KeyboardInterrupt):
            self.logger.info("Exiting...")
            sys.exit()

    @property
    def user(self) -> discord.ClientUser:
        assert super().user, "Bot is not ready yet"
        return typing.cast(discord.ClientUser, super().user)

    @property
    def uptime(self) -> datetime.timedelta:
        return datetime.datetime.utcnow() - self._uptime


class Blueprint:
    version: int
    checksum: bytearray
    width: int
    height: int
    logicImage: bytearray


class InvalidBlueprintException(Exception):
    """Thrown in case of a bad VCB blueprint."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class LogicIcons:
    _logicNames: List[str] = [
        "and", "breakpoint", "buffer", "bus", "clock", "cross", "latchOff", "latchOn",
        "led", "mesh", "nand", "nor", "not", "or", "random", "read", "timer", "tunnel",
        "wireless1", "wireless2", "wireless3", "wireless4", "write", "xnor", "xor",
    ]
    _logicMap: Dict[int, str] = {
        0xFFC663: "and",
        0xE00000: "breakpoint",
        0x92FF63: "buffer",
        0x7A7024: "bus",
        0x24417A: "bus",
        0x25627A: "bus",
        0x3E7A24: "bus",
        0x7A2D66: "bus",
        0x7A2F24: "bus",
        0xFF0041: "clock",
        0x66788E: "cross",
        0x384D47: "latchOff",
        0x63FF9F: "latchOn",
        0xFFFFFF: "led",
        0x646A57: "mesh",
        0xFFA200: "nand",
        0x30D9FF: "nor",
        0xFF628A: "not",
        0x63F2FF: "or",
        0xE5FF00: "random",
        0x2E475D: "read",
        0xFF6700: "timer",
        0x535572: "tunnel",
        0xFF00BF: "wireless1",
        0xFF00AF: "wireless2",
        0xFF009F: "wireless3",
        0xFF008F: "wireless4",
        0x4D383E: "write",
        0xA600FF: "xnor",
        0xAE74FF: "xor",
    }

    def __init__(self, imgDir: str):
        self._resizedImages: List[Dict[int, Image.Image]] = []
        images: Dict[str, Image.Image] = {}
        blendedImages: Dict[int, Image.Image] = {}

        for name in self._logicNames:
            filename = os.path.join(imgDir, f"LogicIcons-{name}.png")
            images[name] = Image.open(filename)

        # Pre-process images for each color in the logic map
        for color, ink in self._logicMap.items():
            icon = images[ink]
            tmp = Image.new("RGBA", icon.size)
            # Fill the image with the RGB color
            tmp.paste(Image.new("RGBA", icon.size, color=(color >> 16, (color >> 8) & 0xFF, color & 0xFF)), (0, 0))
            blendedImages[color] = Image.blend(icon, tmp, 0.4)

        # Pre-render all the possible sizes we might need
        for i in range(0, MAX_ZOOM):
            tmp = {}
            for color, img in blendedImages.items():
                tmp[color] = img.resize((i+1, i+1), Image.Resampling.BICUBIC)
            self._resizedImages.append(tmp)

    def addIcons(self, logic: List[bytearray], img: Image.Image, zoom: int) -> None:
        for yItr in range(0, len(logic)):
            row = logic[yItr]
            for xItr in range(0, len(row)):
                color = int.from_bytes(row[xItr*4 : xItr*4+3], "big")
                if color not in self._logicMap:
                    continue
                icon = self._resizedImages[zoom-1][color]
                x = xItr * zoom
                y = yItr * zoom
                img.alpha_composite(icon, (x, y))


def parseBlueprint(blueprint: str) -> Blueprint:
    blueprint = blueprint.replace("```", "")
    blueprint = blueprint.replace("\'", "")
    if not blueprint.startswith("VCB+") and not blueprint.startswith("bVCB+"):
        raise InvalidBlueprintException("Invalid vcb blueprint - header error")
    if blueprint.startswith("VCB+"):
        blueprint = blueprint[4:] # strip the VCB+
    if blueprint.startswith("bVCB+"):
        blueprint = blueprint[5:] # strip the bVCB+
    try:
        blueprint = base64.b64decode(blueprint)
    except Exception:
        raise InvalidBlueprintException("Invalid vcb blueprint - base64 error")
    version = int.from_bytes(blueprint[0:3], "big")
    checksum = blueprint[3:9]
    width = int.from_bytes(blueprint[9:13], "big")
    height = int.from_bytes(blueprint[13:17], "big")
    if version != 0:
        raise InvalidBlueprintException("Invalid vcb blueprint - unexpected version number: " + str(version))
    if width * height == 0:
        raise InvalidBlueprintException("Invalid vcb blueprint - blueprint is 0x0")
    curpos = 17
    image = None
    while curpos < len(blueprint):
        blockSize = int.from_bytes(blueprint[curpos:curpos+4], "big")
        layerID = int.from_bytes(blueprint[curpos+4:curpos+8], "big")
        imageSize = int.from_bytes(blueprint[curpos+8:curpos+12], "big")
        if blockSize < 12: # also prevents infinite loops on invalid data if blockSize is 0
            raise InvalidBlueprintException("Invalid vcb blueprint - invalid layer block size: " + str(blockSize))
        if layerID == 0: # look for logic layer
            try:
                image = zstd.uncompress(blueprint[curpos+12:curpos+blockSize])
            except Exception:
                raise InvalidBlueprintException("Invalid vcb blueprint - zstd error")
            # validate uncompressed data length
            if len(image) != imageSize:
                raise InvalidBlueprintException("Invalid vcb blueprint - unexpected image size: " + str(imageSize))
        # advance to next block
        curpos += blockSize
    # pack into a Blueprint and return
    bp = Blueprint()
    bp.version = version
    bp.checksum = checksum
    bp.width = width
    bp.height = height
    bp.logicImage = image
    return bp


def getstats(blueprint: str):
    rgba_t = Tuple[int,int,int,int]

    bp = parseBlueprint(blueprint)
    # count pixels in blueprint
    image = bp.logicImage
    counts = {}
    area = 0
    for i in range(0, len(image), 4):
        if image[i+3] > 0:
            rgb = (int(image[i]), int(image[i+1]), int(image[i+2]), int(image[i+3]))
            counts[rgb] = (counts[rgb] if rgb in counts else 0) + 1
            area = area + 1
    # build result message
    totalmessage: List[str] = []
    totalmessage.append("```\n")
    totalmessage.append("checksum: " + bp.checksum.hex() + "\n")
    totalmessage.append("width:    " + str(bp.width) + "\n")
    totalmessage.append("height:   " + str(bp.height) + "\n")
    totalmessage.append("-----------\n")
    tracecount = 0
    buscount = 0

    def percent(n, total):
        return f" ({int(100.0 * n / total + 0.5)}%)"

    def countMessage(name: str, counts: Dict[rgba_t,int], rgba: rgba_t):
        nonlocal tracecount
        nonlocal buscount
        nonlocal totalmessage
        if rgba in counts and counts[rgba] > 0 and not (name.startswith("Bus") or name.startswith("Trace")):
            pct = percent(counts[rgba], bp.width * bp.height)
            totalmessage.append(name + " pixels: " + str(counts[rgba]) + pct + ", ")
        elif name.startswith("Trace")  and rgba in counts:
            tracecount += counts[rgba]
        elif name.startswith("Bus") and rgba in counts:
            buscount += counts[rgba]

    countMessage("Cross", counts, (102, 120, 142, 255))
    countMessage("Tunnel", counts, (83, 85, 114, 255))
    countMessage("Mesh", counts, (100, 106, 87, 255))
    countMessage("Bus1", counts, (122, 47, 36, 255))
    countMessage("Bus2", counts, (62, 122, 36, 255))
    countMessage("Bus3", counts, (36, 65, 122, 255))
    countMessage("Bus4", counts, (37, 98, 122, 255))
    countMessage("Bus5", counts, (122, 45, 102, 255))
    countMessage("Bus6", counts, (122, 112, 36, 255))
    countMessage("Write", counts, (77, 56, 62, 255))
    countMessage("Read", counts, (46, 71, 93, 255))
    countMessage("Trace1", counts, (42, 53, 65, 255))
    countMessage("Trace2", counts, (159, 168, 174, 255))
    countMessage("Trace3", counts, (161, 85, 94, 255))
    countMessage("Trace4", counts, (161, 108, 86, 255))
    countMessage("Trace5", counts, (161, 133, 86, 255))
    countMessage("Trace6", counts, (161, 152, 86, 255))
    countMessage("Trace7", counts, (153, 161, 86, 255))
    countMessage("Trace8", counts, (136, 161, 86, 255))
    countMessage("Trace9", counts, (108, 161, 86, 255))
    countMessage("Trace10", counts, (86, 161, 141, 255))
    countMessage("Trace11", counts, (86, 147, 161, 255))
    countMessage("Trace12", counts, (86, 123, 161, 255))
    countMessage("Trace13", counts, (86, 98, 161, 255))
    countMessage("Trace14", counts, (102, 86, 161, 255))
    countMessage("Trace15", counts, (135, 86, 161, 255))
    countMessage("Trace16", counts, (161, 85, 151, 255))
    countMessage("Buffer", counts, (146, 255, 99, 255))
    countMessage("And", counts, (255, 198, 99, 255))
    countMessage("Or", counts, (99, 242, 255, 255))
    countMessage("Xor", counts, (174, 116, 255, 255))
    countMessage("Not", counts, (255, 98, 138, 255))
    countMessage("Nand", counts, (255, 162, 0, 255))
    countMessage("Nor", counts, (48, 217, 255, 255))
    countMessage("Xnor", counts, (166, 0, 255, 255))
    countMessage("LatchOn", counts, (99, 255, 159, 255))
    countMessage("LatchOff", counts, (56, 77, 71, 255))
    countMessage("Clock", counts, (255, 0, 65, 255))
    countMessage("LED", counts, (255, 255, 255, 255))
    countMessage("Timer", counts, (255, 103, 0, 255))
    countMessage("Random", counts, (229, 255, 0, 255))
    countMessage("Break", counts, (224, 0, 0, 255))
    countMessage("Wifi0", counts, (255, 0, 191, 255))
    countMessage("Wifi1", counts, (255, 0, 175, 255))
    countMessage("Wifi2", counts, (255, 0, 159, 255))
    countMessage("Wifi3", counts, (255, 0, 143, 255))
    countMessage("Annotation", counts, (58, 69, 81, 255))
    countMessage("Filler", counts, (140, 171, 161, 255))
    if tracecount > 0:
        totalmessage.append("Trace pixels: " + str(tracecount) + percent(tracecount, bp.width * bp.height) + ", ")
    if buscount > 0:
        totalmessage.append("Bus pixels: " + str(buscount) + percent(tracecount, bp.width * bp.height) + ", ")
    totalmessage.append("Used area: " + str(area) + percent(area, bp.width * bp.height) + ", ")
    totalmessage.append("```")
    return totalmessage


def render(blueprint: str, icons: LogicIcons) -> None:
    def fillBackground(image, width: int, height: int) -> bytearray:
        image = bytearray(image)
        for offset in range(0, 4*width*height, 4):
            if image[offset+3] == 0:
                image[offset+0] = 30
                image[offset+1] = 36
                image[offset+2] = 49
                image[offset+3] = 255
        return image

    def zoomImage(image: bytearray, width: int, height: int, zoom: int) -> bytearray:
        if zoom == 1:
            return image
        zimage = bytearray(4 * width * height * zoom * zoom)
        stride = 4 * width
        dststride = 4 * width * zoom
        for y in range(height):
            yo = y * stride
            for x in range(width):
                pixel = image[4 * x + yo : 4 * x + yo + 4]
                for dy in range(zoom):
                    dstyo = (y * zoom + dy) * dststride
                    for dx in range(zoom):
                        dsto = dstyo + 4 * (x * zoom + dx)
                        zimage[dsto] = pixel[0]
                        zimage[dsto+1] = pixel[1]
                        zimage[dsto+2] = pixel[2]
                        zimage[dsto+3] = pixel[3]
        return zimage

    def saveImage(filename: str, logic: bytearray, width: int, height: int, zoom) -> None:
        image = fillBackground(logic, width, height)
        image = zoomImage(image, width, height, zoom)
        pimage = Image.frombytes("RGBA", (width * zoom, height * zoom), image)

        if zoom >= 6:
            logic = [logic[i:i+4*width] for i in range(0, len(logic), 4*width)]
            icons.addIcons(logic, pimage, zoom)

        pimage.save(filename)

    bp = parseBlueprint(blueprint)
    #zoom = int(800 / bp.width)
    zoom = int(1400 / bp.width)
    zoom = min(max(zoom, 1), MAX_ZOOM)
    saveImage("tempimage.png", bp.logicImage, bp.width, bp.height, zoom)


def time() -> str:
    timeStr = str(datetime.datetime.utcnow()).replace(".",",")
    timeStr = "[" + str(timeStr[0:23]) + "]"
    return timeStr


async def extractBlueprintString(ctx: commands.Context, args: List[str]) -> str:
    """Extract blueprint string from appropriate source"""
    blueprint = None
    # 1. first check for bp in args
    if len(args) >= 1:
        for text in args:
            if text.startswith("VCB+") or text.startswith("```VCB+"):
                blueprint = text
    # 2. if not found check for bp in attachment
    if blueprint is None and len(ctx.message.attachments) == 1:
        blueprint = (await ctx.message.attachments[0].read()).decode()
    # if not found, look in replied message...
    if blueprint is None and ctx.message.reference is not None and ctx.message.reference.resolved is not None:
        # 3. check reply content
        if ctx.message.reference.resolved.content != "":
            for text in ctx.message.reference.resolved.content.split():
                if text.startswith("VCB+") or text.startswith("```VCB+"):
                    blueprint = text
        # 4. if still no blueprint found, then check attachment
        if blueprint is None and len(ctx.message.reference.resolved.attachments) == 1:
            blueprint = (await ctx.message.reference.resolved.attachments[0].read()).decode()
    return blueprint


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    imgDir = os.path.join(os.path.abspath(os.path.dirname(sys.argv[0])), "img")
    icons = LogicIcons(imgDir)
    bot = CustomBot(prefix="!", activity=discord.Game(name='!help to learn more'))

    @bot.command(aliases=['hi'])
    async def hello(ctx: commands.Context, *args):
        """Says hi :)"""
        print(time() + " INFO: User \"" + str(ctx.author.name) + "\" used: !hello / !hi")
        await ctx.send("Hello! "+ str(ctx.author.mention))

    @bot.command(aliases=['statistics'])
    async def stats(ctx: commands.Context, *blueprint):
        """
        Makes a image of a blueprint

        Parameters
        ----------
        blueprint
             : Any blueprint either a file or text (can reply to a meesage for a image of that blueprint aswell)
        """
        print(time() + " INFO: User \"" + str(ctx.author.name) + "\" used: !stats")
        # extract blueprint string from appropriate source
        blueprint = await extractBlueprintString(ctx, blueprint)
        # build stats/error message
        totalmessage = []
        if blueprint is None:
            totalmessage.append("No blueprint specified")
        else:
            try:
                totalmessage = getstats(blueprint)
            except InvalidBlueprintException as x:
                totalmessage.append(str(x))
        # send the message
        await ctx.send(" ".join(totalmessage))

    @bot.command(aliases=['render'])
    @commands.has_permissions(attach_files=True)
    async def image(ctx: commands.Context, *blueprint):
        """
        Makes a image of a blueprint

        Parameters
        ----------
        blueprint
             : Any blueprint either a file or text (can reply to a meesage for a image of that blueprint aswell)
        """
        print(time() + " INFO: user \"" + str(ctx.author.name) + "\" used: !image")
        # extract blueprint string from appropriate source
        blueprint = await extractBlueprintString(ctx, blueprint)
        # render blueprint and send image
        totalmessage = []
        if blueprint is None:
            totalmessage.append("No blueprint specified")
        else:
            try:
                render(blueprint, icons)
                await ctx.send(file=discord.File("tempimage.png"))
            except InvalidBlueprintException as x:
                totalmessage.append(str(x))
        # send any error messages
        if totalmessage:
            await ctx.send(" ".join(totalmessage))

    @bot.command(aliases=['guide','manual'])
    async def rtfm(ctx: commands.Context, *question):
        """
        Finds pages in the userguide based on a input

        Parameters
        ----------
        question
             : The thing you are looking for
        """
        print(time() + " INFO: User \"" + str(ctx.author.name) + "\" used: !rtfm "+" ".join(question))
        totalmessage = []
        guides = [
            "appendix blueprint specification",
            "assembly assembler",
            "assembly assembly language",
            "assembly bookmarks",
            "assembly expressions",
            "assembly external editing",
            "assembly macros 1",
            "assembly macros 2",
            "assembly origin directive",
            "assembly primitives labels",
            "assembly primitives numerics",
            "assembly primitives pointers",
            "assembly primitives symbols",
            "assembly primitives",
            "assembly review",
            "assembly statements 1",
            "assembly statements 2",
            "editing and simulating",
            "editing array tool",
            "editing blueprints",
            "editing edit mode tips",
            "editing filter",
            "editing layers",
            "editing simulation mode tips",
            "editing tools",
            "introduction editing and simulation",
            "introduction simulation engine",
            "user interface docking system",
            "user interface navigation and shortcuts",
            "user interface right click behavior",
            "virtual circuits annotation ink",
            "virtual circuits bus ink",
            "virtual circuits components and traces",
            "virtual circuits cross ink",
            "virtual circuits drawing based interface",
            "virtual circuits flow control 1",
            "virtual circuits flow control 2",
            "virtual circuits flow control 3",
            "virtual circuits flow control 4",
            "virtual circuits gate components",
            "virtual circuits general components 1",
            "virtual circuits general components 2",
            "virtual circuits mesh ink",
            "virtual circuits multiple io",
            "virtual circuits space optimization",
            "virtual circuits tunnel ink",
            "virtual circuits uncountable connection 1",
            "virtual circuits uncountable connection 2",
            "virtual devices virtual display",
            "virtual devices virtual input",
            "virtual devices virtual memory 1",
            "virtual devices virtual memory 2",
            "virtual devices virtual memory 3",
            "virtual devices",
        ]
        for item in guides:
            if " ".join(question).lower() in item:
                totalmessage.append(str(item))
        if not question:
            await ctx.send("Please provide a query")
        elif len(totalmessage) == 0:
            await ctx.send("Sorry, I couldnt find anything in the user guide")
        elif len(totalmessage) >= 16:
            await ctx.send("Please be more specific")
        elif len(totalmessage) >= 5:
            matched = False
            question = "_".join(question).lower()
            for founditems in totalmessage:
                if question == founditems.replace(" ","_"):
                    await ctx.send(file=discord.File(founditems.replace(" ","_") + ".png"))
                    matched = True
            if not matched:
                await ctx.send("``` " + "\n ".join(totalmessage) + " ```")
        else:
            for image in totalmessage:
                await ctx.send(file=discord.File(image.replace(" ","_") + ".png"))

    bot.run()


if __name__ == "__main__":
    main()
