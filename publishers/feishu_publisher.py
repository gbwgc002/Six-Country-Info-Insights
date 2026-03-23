
import os
import re
import json
import asyncio
import aiohttp
from datetime import datetime, timedelta
from pathlib import Path

class FeishuPublisher:
    """Publish content to Feishu (Lark) Cloud Documents."""

    BASE_URL = "https://open.feishu.cn/open-apis"
    # Document admin - will be granted full access to all created documents
    # TODO: Replace with your own Feishu Open ID
    ADMIN_OPEN_ID = os.environ.get("FEISHU_ADMIN_OPEN_ID", "")
    # Document retention period in days
    RETENTION_DAYS = 180
    # Path to store document records
    DOCUMENTS_DB = Path(__file__).parent.parent / "data" / "documents.json"

    def __init__(self):
        self.app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        # Folder token (optional, not used if can't add app as collaborator)
        self.folder_token = os.environ.get("FEISHU_FOLDER_TOKEN", "").strip()
        self._tenant_access_token = None
        self._token_expiry = 0

    def is_configured(self) -> bool:
        """Check if Feishu credentials are present."""
        return bool(self.app_id and self.app_secret)

    async def _get_tenant_access_token(self) -> str:
        """Get or refresh tenant access token."""
        if self._tenant_access_token and datetime.now().timestamp() < self._token_expiry:
            return self._tenant_access_token

        url = f"{self.BASE_URL}/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    raise Exception(f"Feishu Auth Failed: {await response.text()}")

                data = await response.json()
                if data.get("code") != 0:
                    raise Exception(f"Feishu Auth Error: {data.get('msg')}")

                self._tenant_access_token = data["tenant_access_token"]
                # Expires in 2 hours, refresh slightly earlier
                self._token_expiry = datetime.now().timestamp() + data["expire"] - 300
                return self._tenant_access_token

    async def set_document_public_permission(self, doc_token: str, chat_id: str = None) -> bool:
        """Set document permission to allow group members to read and add admin.

        Args:
            doc_token: The document token/id
            chat_id: Optional chat_id to add as collaborator

        Returns:
            True if successful, False otherwise
        """
        token = await self._get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        members_url = f"{self.BASE_URL}/drive/v1/permissions/{doc_token}/members?type=docx&need_notification=false"
        success = False

        # Add admin user with full access
        if self.ADMIN_OPEN_ID:
            admin_payload = {
                "member_type": "openid",
                "member_id": self.ADMIN_OPEN_ID,
                "perm": "full_access"
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(members_url, json=admin_payload, headers=headers) as response:
                        data = await response.json()
                        if data.get("code") == 0:
                            print(f"   ✅ Added admin with full_access")
                            success = True
                        else:
                            print(f"   ⚠️ Add admin warning: {data.get('msg', '')}")
            except Exception as e:
                print(f"   ⚠️ Add admin error: {e}")

        # Add chat group as viewer
        if chat_id:
            member_payload = {
                "member_type": "openchat",
                "member_id": chat_id,
                "perm": "view"
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(members_url, json=member_payload, headers=headers) as response:
                        data = await response.json()
                        if data.get("code") == 0:
                            print(f"   ✅ Added chat group as document viewer")
                            success = True
                        else:
                            error_msg = data.get('msg', '')
                            print(f"   ⚠️ Add chat member warning: {error_msg}")
            except Exception as e:
                print(f"   ⚠️ Add member error: {e}")

        return success

    async def delete_document(self, doc_token: str) -> bool:
        """Delete a file or document by its token.

        Args:
            doc_token: The file/document token to delete

        Returns:
            True if deleted successfully, False otherwise
        """
        token = await self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        # The API is generic for files, type param is optional but safer to omit for generic files
        url = f"{self.BASE_URL}/drive/v1/files/{doc_token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, headers=headers) as response:
                    data = await response.json()
                    if data.get("code") == 0:
                        print(f"   ✅ Deleted file/document: {doc_token}")
                        return True
                    else:
                        print(f"   ❌ Delete failed: {data.get('msg', '')}")
                        return False
        except Exception as e:
            print(f"   ❌ Delete error: {e}")
            return False

    async def list_app_documents(self) -> list:
        """List all documents created by this app.

        Returns:
            List of document info dicts
        """
        token = await self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        # List files in app's root folder
        url = f"{self.BASE_URL}/drive/v1/files?folder_token=&order_by=EditedTime&direction=DESC&page_size=50"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    data = await response.json()
                    if data.get("code") == 0:
                        files = data.get("data", {}).get("files", [])
                        return files
                    else:
                        print(f"   ⚠️ List files error: {data.get('msg', '')}")
                        return []
        except Exception as e:
            print(f"   ⚠️ List error: {e}")
            return []

    async def create_document(self, title: str) -> str:
        """Create a new Docx and return its document_id."""
        token = await self._get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        # If folder_token is set, create in folder using Drive API
        if self.folder_token:
            url = f"{self.BASE_URL}/drive/v1/files/create_docx"
            payload = {
                "folder_token": self.folder_token,
                "title": title
            }
        else:
            # Create in root using Docx API
            url = f"{self.BASE_URL}/docx/v1/documents"
            payload = {
                "title": title
            }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                data = await response.json()
                if data.get("code") != 0:
                    raise Exception(f"Create Doc Error: {data.get('msg')}")

                # Drive API returns 'file_token' inside 'file', Docx API returns 'document_id' inside 'document'
                # Both are nested inside 'data'
                res_data = data.get("data", {})
                if "file" in res_data: # Drive API response
                    # For Docx created via Drive API, file_token == document_id
                    return res_data["file"]["token"]
                elif "document" in res_data: # Docx API response
                    return res_data["document"]["document_id"]
                else:
                    raise Exception(f"Unknown response format: {data}")

    def _markdown_to_blocks(self, content: str) -> list[dict]:
        """Parse simple Markdown to Feishu Block structure."""
        blocks = []
        lines = content.split('\n')

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Heading 1 (##) - Map to Heading 2 in Feishu for aesthetics
            if line.startswith("## "):
                text = line[3:]
                blocks.append(self._create_block(text, block_type=4)) # Heading 2

            # Heading 2 (###) - Map to Heading 3
            elif line.startswith("### "):
                text = line[4:]
                blocks.append(self._create_block(text, block_type=5)) # Heading 3

            # Bullet list
            elif line.startswith("- ") or line.startswith("* "):
                text = line[2:]
                blocks.append(self._create_block(text, block_type=12)) # Bullet

            # Numbered list (simple regex)
            elif re.match(r'^\d+\.\s', line):
                text = re.sub(r'^\d+\.\s', '', line)
                blocks.append(self._create_block(text, block_type=13)) # Numbered

            # Default text
            else:
                blocks.append(self._create_block(line, block_type=2)) # Text

        return blocks

    def _create_block(self, text: str, block_type: int) -> dict:
        """Create a block object with text elements handling links."""
        # Simple link parsing: [text](url)
        elements = []
        pattern = r'\[([^\]]+)\]\(([^)]+)\)'

        # Iterate and find links
        last_idx = 0
        for match in re.finditer(pattern, text):
            # Text before link
            if match.start() > last_idx:
                elements.append({
                    "text_run": {
                        "content": text[last_idx:match.start()]
                    }
                })

            # Link
            link_text = match.group(1)
            link_url = match.group(2)
            elements.append({
                "text_run": {
                    "content": link_text,
                    "text_element_style": {
                        "link": {"url": link_url}
                    }
                }
            })

            last_idx = match.end()

        # Remaining text
        if last_idx < len(text):
            elements.append({
                "text_run": {
                    "content": text[last_idx:]
                }
            })

        # If no elements were created (empty text), add empty text_run
        if not elements:
            elements.append({
                "text_run": {
                    "content": text
                }
            })

        # Block type to key mapping
        type_mapping = {
            2: "text",
            3: "heading1",
            4: "heading2",
            5: "heading3",
            12: "bullet",
            13: "ordered"
        }

        type_name = type_mapping.get(block_type, "text")

        return {
            "block_type": block_type,
            type_name: {
                "elements": elements
            }
        }

    async def write_content(self, document_id: str, blocks: list[dict]):
        """Append blocks to the document."""
        token = await self._get_tenant_access_token()
        url = f"{self.BASE_URL}/docx/v1/documents/{document_id}/blocks/{document_id}/children"
        headers = {"Authorization": f"Bearer {token}"}

        # Feishu has limits on block creation (e.g. 50 at a time)
        batch_size = 50
        for i in range(0, len(blocks), batch_size):
            batch = blocks[i:i+batch_size]
            payload = {"children": batch}

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    data = await response.json()
                    if data.get("code") != 0:
                        print(f"Error writing blocks batch {i}: {data.get('msg')}")

    async def upload_file(self, file_path: str, file_name: str = None, parent_type: str = "explorer") -> dict:
        """Upload a file to Feishu Drive.

        Requires Permissions:
        - drive:drive (查看、评论、编辑和管理云空间所有文件)
        - OR drive:file:upload (上传文件到云空间)

        Args:
            file_path: Local path to the file
            file_name: Name for the uploaded file (defaults to original filename)
            parent_type: Parent type, "explorer" for app root folder

        Returns:
            Dict with file_token and url, or None on failure
        """
        if not self.is_configured():
            print("Feishu publisher not configured (missing APP_ID/SECRET)")
            return None

        token = await self._get_tenant_access_token()

        if not file_name:
            file_name = Path(file_path).name

        # Get file size
        file_size = Path(file_path).stat().st_size

        url = f"{self.BASE_URL}/drive/v1/files/upload_all"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            with open(file_path, "rb") as f:
                # Use FormData for multipart upload
                form_data = aiohttp.FormData()
                form_data.add_field("file_name", file_name)
                form_data.add_field("parent_type", parent_type)
                form_data.add_field("parent_node", self.folder_token or "")
                form_data.add_field("size", str(file_size))
                form_data.add_field("file", f, filename=file_name, content_type="application/pdf")

                async with aiohttp.ClientSession() as session:
                    async with session.post(url, data=form_data, headers=headers) as response:
                        data = await response.json()
                        if data.get("code") != 0:
                            msg = data.get('msg')
                            print(f"   ❌ Upload failed: {msg}")
                            if "permission" in str(msg).lower() or "access denied" in str(msg).lower():
                                print("   💡 Check permissions: 'drive:drive' or 'drive:file:upload' is required.")
                                print("   💡 Remember to release a new version of your app after adding permissions!")
                            return None

                        file_token = data.get("data", {}).get("file_token")
                        if file_token:
                            file_url = f"https://feishu.cn/file/{file_token}"
                            print(f"   ✅ File uploaded: {file_url}")
                            return {"file_token": file_token, "url": file_url}
                        return None

        except Exception as e:
            print(f"   ❌ Upload error: {e}")
            return None

    async def set_file_permission(self, file_token: str, chat_id: str = None) -> bool:
        """Set file permission for chat group and admin.

        Args:
            file_token: The file token
            chat_id: Optional chat_id to add as viewer

        Returns:
            True if successful
        """
        token = await self._get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        members_url = f"{self.BASE_URL}/drive/v1/permissions/{file_token}/members?type=file&need_notification=false"
        success = False

        # Add admin user with full access
        if self.ADMIN_OPEN_ID:
            admin_payload = {
                "member_type": "openid",
                "member_id": self.ADMIN_OPEN_ID,
                "perm": "full_access"
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(members_url, json=admin_payload, headers=headers) as response:
                        data = await response.json()
                        if data.get("code") == 0:
                            print(f"   ✅ Added admin with full_access to file")
                            success = True
                        else:
                            print(f"   ⚠️ Add admin to file warning: {data.get('msg', '')}")
            except Exception as e:
                print(f"   ⚠️ Add admin to file error: {e}")

        # Add chat group as viewer
        if chat_id:
            member_payload = {
                "member_type": "openchat",
                "member_id": chat_id,
                "perm": "view"
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(members_url, json=member_payload, headers=headers) as response:
                        data = await response.json()
                        if data.get("code") == 0:
                            print(f"   ✅ Added chat group as file viewer")
                            success = True
                        else:
                            print(f"   ⚠️ Add chat to file warning: {data.get('msg', '')}")
            except Exception as e:
                print(f"   ⚠️ Add chat to file error: {e}")

        return success

    async def upload_pdf(self, pdf_path: str, title: str, chat_id: str = None) -> str:
        """Upload PDF and set permissions.

        Args:
            pdf_path: Local path to PDF file
            title: Title for the file
            chat_id: Chat ID for permission granting

        Returns:
            URL to access the PDF, or None on failure
        """
        if not self.is_configured():
            print("Feishu publisher not configured (missing APP_ID/SECRET)")
            return None

        try:
            print(f"📄 Uploading PDF to Feishu: {title}...")
            result = await self.upload_file(pdf_path, f"{title}.pdf")

            if not result:
                return None

            file_token = result["file_token"]

            # Set permissions
            print("   Setting file permissions...")
            await self.set_file_permission(file_token, chat_id)

            # Record for cleanup
            self._record_document(file_token, title)

            return result["url"]

        except Exception as e:
            print(f"❌ PDF Upload Error: {e}")
            return None

    async def publish(self, title: str, markdown_content: str, chat_id: str = None) -> str:
        """Main method: Create doc and write content.

        Args:
            title: Document title
            markdown_content: Content in markdown format
            chat_id: Optional chat_id to grant read permission
        """
        if not self.is_configured():
            print("Feishu publisher not configured (missing APP_ID/SECRET)")
            return None

        try:
            print(f"Creating Feishu document: {title}...")
            doc_id = await self.create_document(title)

            # Set document permission - try to add chat group as viewer
            print("Setting document permissions...")
            await self.set_document_public_permission(doc_id, chat_id)

            print("Parsing content...")
            blocks = self._markdown_to_blocks(markdown_content)

            print(f"Writing {len(blocks)} blocks to document...")
            await self.write_content(doc_id, blocks)

            # Use the correct user-accessible document URL format
            doc_url = f"https://feishu.cn/docx/{doc_id}"
            print(f"✅ Published to Feishu: {doc_url}")

            # Record document for future cleanup
            self._record_document(doc_id, title)

            return doc_url

        except Exception as e:
            print(f"❌ Feishu Publish Error: {e}")
            return None

    def _record_document(self, doc_token: str, title: str):
        """Record document info for future cleanup."""
        try:
            # Ensure data directory exists
            self.DOCUMENTS_DB.parent.mkdir(parents=True, exist_ok=True)

            # Load existing records
            if self.DOCUMENTS_DB.exists():
                with open(self.DOCUMENTS_DB, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {"documents": []}

            # Add new record
            data["documents"].append({
                "token": doc_token,
                "title": title,
                "created_at": datetime.now().isoformat()
            })

            # Save
            with open(self.DOCUMENTS_DB, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        except Exception as e:
            print(f"   ⚠️ Failed to record document: {e}")

    async def cleanup_old_documents(self) -> int:
        """Delete documents older than RETENTION_DAYS.

        Returns:
            Number of documents deleted
        """
        if not self.DOCUMENTS_DB.exists():
            return 0

        try:
            with open(self.DOCUMENTS_DB, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"   ⚠️ Failed to load document records: {e}")
            return 0

        cutoff_date = datetime.now() - timedelta(days=self.RETENTION_DAYS)
        deleted_count = 0
        remaining_docs = []

        for doc in data.get("documents", []):
            created_at = datetime.fromisoformat(doc["created_at"])

            if created_at < cutoff_date:
                # Delete old document
                print(f"   🗑️ Cleaning up old document: {doc['title']}")
                success = await self.delete_document(doc["token"])
                if success:
                    deleted_count += 1
                else:
                    # Keep in list if deletion failed
                    remaining_docs.append(doc)
            else:
                remaining_docs.append(doc)

        # Update records
        data["documents"] = remaining_docs
        with open(self.DOCUMENTS_DB, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        if deleted_count > 0:
            print(f"   ✅ Cleaned up {deleted_count} old documents")

        return deleted_count

    async def _send_message(self, receive_id: str, msg_type: str, content: str):
        """Send a message via Feishu IM API."""
        token = await self._get_tenant_access_token()
        url = f"{self.BASE_URL}/im/v1/messages?receive_id_type=chat_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        payload = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": content
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                data = await response.json()
                if data.get("code") != 0:
                    print(f"Feishu Send Message Error: {data.get('msg')} (code {data.get('code')})")
                else:
                    print(f"✅ Feishu message sent to {receive_id}")

    def _build_card_content(self, title: str, highlights: str, categories: dict, category_names: dict, doc_url: str = None) -> str:
        """Construct Feishu Interactive Card JSON content.

        Args:
            title: Card title
            highlights: Today's highlights text (top 3 eye-catching items)
            categories: Dict of category -> list of NewsItem (unused in simplified card)
            category_names: Dict of category_id -> display name (unused in simplified card)
            doc_url: Optional URL to the full document for click-through
        """
        elements = []

        # Only show highlights - top 3 eye-catching items
        if highlights:
            # Clean HTML tags if present (simple regex)
            clean_highlights = re.sub(r'<[^>]+>', '', highlights).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**⚡ 今日要点**\n\n{clean_highlights}"
                }
            })

        # Action button to view full document (if doc_url provided)
        if doc_url:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "📖 查看完整内容"
                        },
                        "type": "primary",
                        "multi_url": {
                            "url": doc_url,
                            "pc_url": doc_url,
                            "ios_url": doc_url,
                            "android_url": doc_url
                        }
                    }
                ]
            })

        # Footer / Note
        elements.append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "Generated by Six-Country Info Insights"
                }
            ]
        })

        card = {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": title
                }
            },
            "elements": elements
        }

        return json.dumps(card)

    async def send_digest_card(self, chat_id: str, title: str, highlights: str, categories: dict, category_names: dict, doc_url: str = None):
        """Send the news digest as an interactive card.

        Args:
            chat_id: Feishu chat ID to send to
            title: Card title
            highlights: Today's highlights text
            categories: Dict of category -> list of NewsItem
            category_names: Dict of category_id -> display name
            doc_url: Optional URL to the full document for click-through
        """
        if not self.is_configured():
             print("Feishu publisher not configured.")
             return

        print(f"Sending Feishu card to {chat_id}...")
        card_content = self._build_card_content(title, highlights, categories, category_names, doc_url)
        await self._send_message(chat_id, "interactive", card_content)

