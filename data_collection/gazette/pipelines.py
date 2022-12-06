import datetime as dt
import hashlib
import re
from pathlib import Path

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem
from scrapy.http import Request
from scrapy.pipelines.files import FilesPipeline
from scrapy.settings import Settings
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from gazette.database.models import Gazette, initialize_database

SP_RIBEIRAO_PRETO_PDF_FILENAME: re.Pattern[bytes] = re.compile(rb'filename="([^"]+)"')


class GazetteDateFilteringPipeline:
    def process_item(self, item, spider):
        if hasattr(spider, "start_date"):
            if spider.start_date > item.get("date"):
                raise DropItem("Droping all items before {}".format(spider.start_date))
        return item


class DefaultValuesPipeline:
    """Add defaults values field, if not already set in the item"""

    default_field_values = {
        "territory_id": lambda spider: getattr(spider, "TERRITORY_ID"),
        "scraped_at": lambda spider: dt.datetime.utcnow(),
    }

    def process_item(self, item, spider):
        for field in self.default_field_values:
            if field not in item:
                item[field] = self.default_field_values.get(field)(spider)
        return item


class SQLDatabasePipeline:
    def __init__(self, database_url):
        self.database_url = database_url

    @classmethod
    def from_crawler(cls, crawler):
        database_url = crawler.settings.get("QUERIDODIARIO_DATABASE_URL")
        return cls(database_url=database_url)

    def open_spider(self, spider):
        if self.database_url is not None:
            engine = initialize_database(self.database_url)
            self.Session = sessionmaker(bind=engine)

    def process_item(self, item, spider):
        if self.database_url is None:
            return item

        session = self.Session()

        fields = [
            "source_text",
            "date",
            "edition_number",
            "is_extra_edition",
            "power",
            "scraped_at",
            "territory_id",
        ]
        gazette_item = {field: item.get(field) for field in fields}

        for file_info in item.get("files", []):
            already_downloaded = file_info["status"] == "uptodate"
            if already_downloaded:
                # We should not insert in database information of
                # files that were already downloaded before
                continue

            gazette_item["file_path"] = file_info["path"]
            gazette_item["file_url"] = file_info["url"]
            gazette_item["file_checksum"] = file_info["checksum"]

            gazette = Gazette(**gazette_item)
            session.add(gazette)
            try:
                session.commit()
            except SQLAlchemyError as exc:
                spider.logger.warning(
                    f"Something wrong has happened when adding the gazette in the database. "
                    f"Date: {gazette_item['date']}. "
                    f"File Checksum: {gazette_item['file_checksum']}. "
                    f"Details: {exc.args}"
                )
                session.rollback()

        session.close()

        return item


class QueridoDiarioFilesPipeline(FilesPipeline):
    """Pipeline to download files described in file_urls or file_requests item fields.

    The main differences from the default FilesPipelines is that this pipeline:
        - organizes downloaded files differently (based on territory_id)
        - adds the file_requests item field to download files from request instances
        - allows a download_file_headers spider attribute to modify file_urls requests
    """

    DEFAULT_FILES_REQUESTS_FIELD = "file_requests"

    def __init__(self, *args, settings=None, **kwargs):
        super().__init__(*args, settings=settings, **kwargs)

        if isinstance(settings, dict) or settings is None:
            settings = Settings(settings)

        self.files_requests_field = settings.get(
            "FILES_REQUESTS_FIELD", self.DEFAULT_FILES_REQUESTS_FIELD
        )

    def get_media_requests(self, item, info):
        """Makes requests from urls and/or lets through ready requests."""
        urls = ItemAdapter(item).get(self.files_urls_field, [])
        download_file_headers = getattr(info.spider, "download_file_headers", {})
        yield from (Request(u, headers=download_file_headers) for u in urls)

        requests = ItemAdapter(item).get(self.files_requests_field, [])
        yield from requests

    def item_completed(self, results, item, info):
        """
        Transforms requests into strings if any present.
        Default behavior also adds results to item.
        """
        requests = ItemAdapter(item).get(self.files_requests_field, [])
        if requests:
            ItemAdapter(item)[self.files_requests_field] = [
                f"{r.method} {r.url}" for r in requests
            ]

        return super().item_completed(results, item, info)

    def file_path(self, request, response=None, info=None, item=None):
        """
        Path to save the files, modified to organize the gazettes in directories.
        The files will be under <territory_id>/<gazette date>/.
        """

        datestr = item["date"].strftime("%Y-%m-%d")

        if response and item["territory_id"] == "3543402":
            if content_disposition := response.headers.get("Content-Disposition"):
                if SP_RIBEIRAO_PRETO_PDF_FILENAME.search(content_disposition):
                    return self.file_path_for_sp_ribeirao_preto(
                        request,
                        response=response,
                        info=info,
                        item=item,
                    )
                else:
                    info.spider.logger.info(
                        f"Unable to extract the actual PDF file name for {datestr}"
                        " entry of territory_id 3543402. Falling back to"
                        " request.url-based filename calculation"
                    )
            else:
                info.spider.logger.info(
                    f"Unable to extract Content-Disposition header for {datestr}"
                    " entry of territory_id 3543402. Falling back to"
                    " request.url-based filename calculation"
                )

        filepath = super().file_path(request, response=response, info=info, item=item)
        # The default path from the scrapy class begins with "full/". In this
        # class we replace that with the territory_id and gazette date.
        filename = Path(filepath).name
        if item["territory_id"] == "3543402":
            # For sp_ribeirao_preto, the URL ends with .xhtml but content is a PDF file
            # We will remove the extension included by FilesPipeline.file_path code
            filename = Path(filepath).stem
        return str(Path(item["territory_id"], datestr, filename))

    def file_path_for_sp_ribeirao_preto(self, request, response, info=None, item=None):
        """
        sp_ribeirao_preto source requires a FormRequest for the same URL, but
        the actual filename is in the `Content-Disposition` entry of
        the response headers.

        We had to customize file_path for this spider due to cases
        when there are multiple files for the same date.
        """

        content_disposition: bytes = response.headers["Content-Disposition"]
        match_: re.Match[bytes] = SP_RIBEIRAO_PRETO_PDF_FILENAME.search(
            content_disposition
        )
        pdf_filename: bytes = match_.group(1)

        datestr: str = item["date"].strftime("%Y-%m-%d")
        filename: str = hashlib.sha1(pdf_filename).hexdigest()
        return str(Path(item["territory_id"], datestr, filename))
