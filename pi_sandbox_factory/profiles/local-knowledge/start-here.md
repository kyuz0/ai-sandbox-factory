Ask about the files in your workspace or mounted folders.

Try “Which files explain how to run this project? Cite the relevant sections.”

- Add your documents under Folders during setup or in Settings. Keep the folder read-only and name it `reference`. Tell Pi to look in `/mnt/reference`.
- Under **Network and Web Search**, choose **Restricted** and leave **Web Search** off to search only your documents.
- Keyword search is ready to use. For semantic search, open **Document search → Configure…** in Factory. Choose **Semantic search with pi-knowledge** and pick a model from your provider or enter your server’s details.
- In chat, run `/knowledge-index /mnt/reference` to send that folder to the embedding provider you chose.

[Read the full Local Knowledge guide](https://github.com/kyuz0/pi-sandbox-factory/blob/main/docs/local-knowledge.md).
