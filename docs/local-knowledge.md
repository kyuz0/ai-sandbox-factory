# Local Knowledge

Use **Local Knowledge** to ask Pi questions about your documents. Its
`local-knowledge` skill searches by file name and text and cites the sources.
You can start with a folder. Keyword search needs no index or embedding model.

## Search a document folder

1. Choose **New sandbox**, keep **Pi** as the coding agent, and select the **Local Knowledge** profile. Pick a writable **Workspace** for
   reports and notes, then choose **Continue**.
2. Under **Folders → Add folder…**, select your documents. Name the mount
   `reference` and leave **Read-only** checked. Pi will see it at `/mnt/reference`.
3. Under **Network and Web Search**, leave **Web Search** off. Choose **Restricted**
   networking and leave website categories and additional destinations empty.
   [Network rules](web-access.md#network-access)
4. Choose **Create and start sandbox**.

Try asking “Which files in `/mnt/reference` explain how to onboard a new team
member? Cite the file and section.” Type `/profile-guide` for the profile’s
instructions in chat.

![Local Knowledge setup with the documents mounted read-only](research-local-folders.png)

## Turn embeddings on

Semantic search finds related passages even when the wording differs. Factory
installs `pi-knowledge`. When you index files, Pi sends passages to the embedding
provider you choose. When you search the index, Pi sends your query to the same
provider. Factory downloads no embedding model. [pi-knowledge package](https://pi.dev/packages/pi-knowledge)

1. On **Review sandbox**, expand **Agent tools** and choose **Configure…** beside **Document search**. For an
   existing sandbox, open **Settings → Document search → Configure…**.
2. Set **Search mode** to **Semantic search with pi-knowledge**, then choose where to run it.
   - **Connected provider.** Pick OpenRouter or a supported provider plugin under
     **Embedding provider**, then choose an **Embedding model**. Factory reuses
     the connection. If the list is unavailable, enter a model ID from the provider.
   - **Custom server.** Enter the **Embedding server URL** and **Embedding model ID**.
     Use the API base URL, such as `https://models.example.org/v1`. Factory adds
     `/embeddings`. [Custom server screenshot](research-custom-server.png)
3. Choose **Save**, then **Create and start sandbox** for a new sandbox or **Apply and
   recreate** for an existing one. Applying restarts the sandbox.

![The Document search row and its Configure button in sandbox setup](research-document-search.png)

![Semantic search settings in the Document search dialog](research-pi-knowledge.png)

For a custom server, use an address the sandbox can reach.

| Where the model runs | How to connect |
| --- | --- |
| Your workstation | Enable the server port under **Model Providers → Local models**. Use `http://local-model.internal:PORT/v1`. |
| Another server that accepts requests without a key | Enter its API base URL. In Restricted mode, allow its host and port under **Network and Web Search → Network**. |

If a provider plugin supports embedding models but is missing from the picker,
update it under **Library → Providers**. For an existing sandbox, select
**Actions… → Update sources and recreate…** to load the update. If that sandbox
uses the old offline model, switch to keyword search first. [Updating a sandbox](usage.md#update-factory-and-sandboxes)

### Index and search

In Pi or FrostPi, run `/knowledge-index /mnt/reference`. You can also give it a
single file. Wait for indexing to finish, then ask Pi to search the index and
cite the files. After editing the sources or switching models, run
`/knowledge-refresh reference`.

## Turn embeddings off

Open **Settings → Document search → Configure…**, set **Search mode** to
**Keyword search**, and choose **Save → Apply and recreate**. Finish any running
work before applying. Reopen Pi or FrostPi and ask about your files as before.

Pi now searches the files directly. Document search stops contacting the embedding
server. The index, including excerpts from your files, stays in the sandbox’s
private home. Turn embeddings on with the same server and model to use it again.
