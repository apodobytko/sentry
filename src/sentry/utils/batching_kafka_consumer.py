import abc
import logging
import time
from typing import List

from confluent_kafka import (
    OFFSET_BEGINNING,
    OFFSET_END,
    OFFSET_INVALID,
    OFFSET_STORED,
    Consumer,
    KafkaError,
    KafkaException,
)
from confluent_kafka.admin import AdminClient
from django.conf import settings

from sentry.utils import kafka_config

logger = logging.getLogger("sentry.batching-kafka-consumer")

DEFAULT_QUEUED_MAX_MESSAGE_KBYTES = 50000
DEFAULT_QUEUED_MIN_MESSAGES = 10000


def wait_for_topics(admin_client: AdminClient, topics: List[str], timeout: int = 10) -> None:
    """
    Make sure that the provided topics exist and have non-zero partitions in them.
    """
    for topic in topics:
        start = time.time()
        last_error = None

        while True:
            if time.time() > start + timeout:
                raise RuntimeError(
                    f"Timeout when waiting for Kafka topic '{topic}' to become available, last error: {last_error}"
                )

            result = admin_client.list_topics(topic=topic)
            topic_metadata = result.topics.get(topic)
            if topic_metadata and topic_metadata.partitions and not topic_metadata.error:
                logger.debug("Topic '%s' is ready", topic)
                break
            elif topic_metadata.error in {
                KafkaError.UNKNOWN_TOPIC_OR_PART,
                KafkaError.LEADER_NOT_AVAILABLE,
            }:
                last_error = topic_metadata.error
                logger.warning("Topic '%s' or its partitions are not ready, retrying...", topic)
                time.sleep(0.1)
                continue
            else:
                raise RuntimeError(
                    "Unknown error when waiting for Kafka topic '%s': %s"
                    % (topic, topic_metadata.error)
                )


def create_topics(cluster_name: str, topics: List[str]):
    """If configured to do so, create topics and make sure that they exist

    topics must be from the same cluster.
    """
    if settings.KAFKA_CONSUMER_AUTO_CREATE_TOPICS:
        conf = kafka_config.get_kafka_admin_cluster_options(cluster_name)
        admin_client = AdminClient(conf)
        wait_for_topics(admin_client, topics)


class KafkaConsumerFacade(abc.ABC):
    """
    Kafka consumer facade which defines the minimal set of methods to be implemented in order to be used as a consumer
    with BatchingKafkaConsumer. Additional documentation of the API's defined in this class can be found at
    https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html#pythonclient-consumer
    """

    @abc.abstractmethod
    def subscribe(self, topics, on_assign=None, on_revoke=None):
        """
        Set subscription to supplied list of topics. on_assign and on_revoke are callbacks which would be called when
        topics are assigned or revoked.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def poll(self, timeout):
        """
        Consume a single message from the topic. timeout provides the about of time to wait for a message before
        returning.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def commit(self, *args, **kwargs):
        """
        Commit list of offsets.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def close(self):
        """
        Close down and terminate the Kafka Consumer.
        """
        raise NotImplementedError


class AbstractBatchWorker(metaclass=abc.ABCMeta):
    """The `BatchingKafkaConsumer` requires an instance of this class to
    handle user provided work such as processing raw messages and flushing
    processed batches to a custom backend."""

    @abc.abstractmethod
    def process_message(self, message):
        """Called with each (raw) Kafka message, allowing the worker to do
        incremental (preferably local!) work on events. The object returned
        is put into the batch maintained by the `BatchingKafkaConsumer`.

        If this method returns `None` it is not added to the batch.

        A simple example would be decoding the JSON value and extracting a few
        fields.
        """

    @abc.abstractmethod
    def flush_batch(self, batch):
        """Called with a list of pre-processed (by `process_message`) objects.
        The worker should write the batch of processed messages into whatever
        store(s) it is maintaining. Afterwards the Kafka offsets are committed.

        A simple example would be writing the batch to another Kafka topic.
        """

    @abc.abstractmethod
    def shutdown(self):
        """Called when the `BatchingKafkaConsumer` is shutting down (because it
        was signalled to do so). Provides the worker a chance to do any final
        cleanup.

        A simple example would be closing any remaining backend connections."""


class BatchingKafkaConsumer:
    """
    This consumer is deprecated and will eventually be removed. If you are writing a new
    consumer, use Arroyo.

    The `BatchingKafkaConsumer` is an abstraction over most Kafka consumer's main event
    loops. For this reason it uses inversion of control: the user provides an implementation
    for the `AbstractBatchWorker` and then the `BatchingKafkaConsumer` handles the rest.

    Main differences from the default KafkaConsumer are as follows:
    * Messages are processed locally (e.g. not written to an external datastore!) as they are
      read from Kafka, then added to an in-memory batch
    * Batches are flushed based on the batch size or time sent since the first message
      in the batch was received (e.g. "500 items or 1000ms")
    * Kafka offsets are not automatically committed! If they were, offsets might be committed
      for messages that are still sitting in an in-memory batch, or they might *not* be committed
      when messages are sent to an external datastore right before the consumer process dies
    * Instead, when a batch of items is flushed they are written to the external datastore and
      then Kafka offsets are immediately committed (in the same thread/loop)
    * Users need only provide an implementation of what it means to process a raw message
      and flush a batch of events
    * Supports an optional "dead letter topic" where messages that raise an exception during
      `process_message` are sent so as not to block the pipeline.

    A note on commit_on_shutdown parameter
    If the process_message method of the worker provided to BatchingKafkaConsumer just works
    with in memory stuff and does not influence/modify any external systems, then its ok to
    keep the flag to False. But if the process_message method of the worker influences/modifies
    any external systems then its necessary to set it to True to avoid duplicate work on the
    external systems.
    Example:
        1. Worker process which deserializes the message and extracts a few needed parameters
        can leave the commit_on_shutdown flag to False. This is ok since the next consumer which
        picks up the work will rebuild its state from the messages which have not been committed.
        2. Worker process which sends tasks to celery based on the message needs to set
        commit_on_shutdown to True to avoid duplicate work.
    This is different than the note below since crash scenarios are harder to handle and its ok for
    duplicates to occur in crash cases.

    NOTE: This does not eliminate the possibility of duplicates if the consumer process
    crashes between writing to its backend and commiting Kafka offsets. This should eliminate
    the possibility of *losing* data though. An "exactly once" consumer would need to store
    offsets in the external datastore and reconcile them on any partition rebalance.
    """

    # Set of logical (not literal) offsets to not publish to the commit log.
    # https://github.com/confluentinc/confluent-kafka-python/blob/443177e1c83d9b66ce30f5eb8775e062453a738b/tests/test_enums.py#L22-L25
    LOGICAL_OFFSETS = frozenset([OFFSET_BEGINNING, OFFSET_END, OFFSET_STORED, OFFSET_INVALID])

    # Set of error codes that can be returned by ``consumer.poll`` calls which
    # are generally able to be recovered from after a series of retries.
    RECOVERABLE_ERRORS = frozenset(
        [KafkaError._PARTITION_EOF, KafkaError._TRANSPORT]  # Local: Broker transport failure
    )

    def __init__(
        self,
        topics,
        worker,
        max_batch_size,
        max_batch_time,
        consumer=None,
        cluster_name=None,
        group_id=None,
        metrics=None,
        producer=None,
        dead_letter_topic=None,
        commit_log_topic=None,
        auto_offset_reset="error",
        queued_max_messages_kbytes=None,
        queued_min_messages=DEFAULT_QUEUED_MIN_MESSAGES,
        metrics_sample_rates=None,
        metrics_default_tags=None,
        commit_on_shutdown: bool = False,
    ):
        assert isinstance(worker, AbstractBatchWorker)
        self.worker = worker

        self.max_batch_size = max_batch_size
        self.max_batch_time = max_batch_time  # in milliseconds
        self.__metrics = metrics
        self.__metrics_sample_rates = (
            metrics_sample_rates if metrics_sample_rates is not None else {}
        )
        self.__metrics_default_tags = metrics_default_tags or {}
        self.group_id = group_id
        self.commit_on_shutdown = commit_on_shutdown

        self.shutdown = False

        self.__batch_results = []
        self.__batch_offsets = {}  # (topic, partition) = [low, high]
        self.__batch_deadline = None
        self.__batch_messages_processed_count = 0
        # the total amount of time, in milliseconds, that it took to process
        # the messages in this batch (does not included time spent waiting for
        # new messages)
        self.__batch_processing_time_ms = 0.0

        if isinstance(topics, (tuple, set)):
            topics = list(topics)
        elif not isinstance(topics, list):
            topics = [topics]

        if queued_max_messages_kbytes is None:
            queued_max_messages_kbytes = DEFAULT_QUEUED_MAX_MESSAGE_KBYTES

        self.producer = producer
        self.commit_log_topic = commit_log_topic
        self.dead_letter_topic = dead_letter_topic
        if not consumer:
            self.consumer = self.create_consumer(
                topics,
                cluster_name,
                group_id,
                auto_offset_reset,
                queued_max_messages_kbytes,
                queued_min_messages,
            )
        else:
            assert isinstance(consumer, KafkaConsumerFacade)
            self.consumer = consumer

        def on_partitions_assigned(consumer, partitions):
            logger.info("New partitions assigned: %r", partitions)

        def on_partitions_revoked(consumer, partitions):
            "Reset the current in-memory batch, letting the next consumer take over where we left off."
            logger.info("Partitions revoked: %r", partitions)
            self._flush(force=True)

        self.consumer.subscribe(
            topics, on_assign=on_partitions_assigned, on_revoke=on_partitions_revoked
        )

    def __record_timing(self, metric, value, tags=None):
        if self.__metrics is None:
            return

        tags = dict(tags or ())
        tags.update(self.__metrics_default_tags)

        sample_rate = self.__metrics_sample_rates.get(metric, settings.SENTRY_METRICS_SAMPLE_RATE)
        return self.__metrics.timing(metric, value, tags=tags, sample_rate=sample_rate)

    def create_consumer(
        self,
        topics,
        cluster_name,
        group_id,
        auto_offset_reset,
        queued_max_messages_kbytes,
        queued_min_messages,
    ):
        consumer_config = kafka_config.get_kafka_consumer_cluster_options(
            cluster_name,
            override_params={
                "enable.auto.commit": False,
                "group.id": group_id,
                "default.topic.config": {"auto.offset.reset": auto_offset_reset},
                # overridden to reduce memory usage when there's a large backlog
                "queued.max.messages.kbytes": queued_max_messages_kbytes,
                "queued.min.messages": queued_min_messages,
            },
        )

        create_topics(cluster_name, topics)

        consumer = Consumer(consumer_config)

        return consumer

    def run(self):
        """
        The main run loop, see class docstring for more information.
        """

        logger.debug("Starting")
        while not self.shutdown:
            self._run_once()

        self._shutdown()

    def _run_once(self):
        self._flush()

        if self.producer:
            self.producer.poll(0.0)

        msg = self.consumer.poll(timeout=1.0)

        if msg is None:
            return
        if msg.error():
            if msg.error().code() in self.RECOVERABLE_ERRORS:
                return
            else:
                raise Exception(msg.error())

        self._handle_message(msg)

    def signal_shutdown(self):
        """Tells the `BatchingKafkaConsumer` to shutdown on the next run loop iteration.
        Typically called from a signal handler."""
        logger.debug("Shutdown signalled")

        self.shutdown = True

    def _handle_message(self, msg):
        start = time.time()

        # set the deadline only after the first message for this batch is seen
        if not self.__batch_deadline:
            self.__batch_deadline = self.max_batch_time / 1000.0 + start

        try:
            result = self.worker.process_message(msg)
        except Exception:
            if self.dead_letter_topic:
                logger.exception("Error handling message, sending to dead letter topic.")
                self.producer.produce(
                    self.dead_letter_topic,
                    key=msg.key(),
                    value=msg.value(),
                    headers={
                        "partition": str(msg.partition()) if msg.partition() else None,
                        "offset": str(msg.offset()) if msg.offset() else None,
                        "topic": msg.topic(),
                    },
                    on_delivery=self._commit_message_delivery_callback,
                )
            else:
                raise
        else:
            if result is not None:
                self.__batch_results.append(result)
        finally:
            duration = (time.time() - start) * 1000
            self.__batch_messages_processed_count += 1
            self.__batch_processing_time_ms += duration
            self.__record_timing("process_message", duration)

            topic_partition_key = (msg.topic(), msg.partition())
            if topic_partition_key in self.__batch_offsets:
                self.__batch_offsets[topic_partition_key][1] = msg.offset()
            else:
                self.__batch_offsets[topic_partition_key] = [msg.offset(), msg.offset()]

    def _shutdown(self):
        logger.debug("Stopping")

        if self.commit_on_shutdown:
            self._flush(force=True)
        else:
            # drop in-memory events, letting the next consumer take over where we left off
            self._reset_batch()

        # tell the consumer to shutdown, and close the consumer
        logger.debug("Stopping worker")
        self.worker.shutdown()
        logger.debug("Stopping consumer")
        self.consumer.close()
        if self.dead_letter_topic:
            logger.debug("Stopping producer")
            self.producer.close()
        logger.debug("Stopped")

    def _reset_batch(self):
        logger.debug("Resetting in-memory batch")
        self.__batch_results = []
        self.__batch_offsets = {}
        self.__batch_deadline = None
        self.__batch_messages_processed_count = 0
        self.__batch_processing_time_ms = 0.0

    def _flush(self, force=False):
        """Decides whether the `BatchingKafkaConsumer` should flush because of either
        batch size or time. If so, delegate to the worker, clear the current batch,
        and commit offsets to Kafka."""
        if not self.__batch_messages_processed_count > 0:
            return  # No messages were processed, so there's nothing to do.

        batch_by_size = len(self.__batch_results) >= self.max_batch_size
        batch_by_time = self.__batch_deadline and time.time() > self.__batch_deadline
        if not (force or batch_by_size or batch_by_time):
            return

        logger.info(
            "Flushing %s items (from %r): forced:%s size:%s time:%s",
            len(self.__batch_results),
            self.__batch_offsets,
            force,
            batch_by_size,
            batch_by_time,
        )

        self.__record_timing(
            "process_message.normalized",
            self.__batch_processing_time_ms / self.__batch_messages_processed_count,
        )

        batch_results_length = len(self.__batch_results)
        self.__record_timing("batching_consumer.batch.size", batch_results_length)
        if batch_results_length > 0:
            logger.debug("Flushing batch via worker")
            flush_start = time.time()
            self.worker.flush_batch(self.__batch_results)
            flush_duration = (time.time() - flush_start) * 1000
            logger.info("Worker flush took %dms", flush_duration)
            self.__record_timing("batching_consumer.batch.flush", flush_duration)
            self.__record_timing(
                "batching_consumer.batch.flush.normalized", flush_duration / batch_results_length
            )

        logger.debug("Committing Kafka offsets")
        commit_start = time.time()
        self._commit()
        commit_duration = (time.time() - commit_start) * 1000
        logger.debug("Kafka offset commit took %dms", commit_duration)

        self._reset_batch()

    def _commit_message_delivery_callback(self, error, message):
        if error is not None:
            raise Exception(error.str())

    def _commit(self):
        retries = 3
        while True:
            try:
                offsets = self.consumer.commit(asynchronous=False)
                logger.debug("Committed offsets: %s", offsets)
                break  # success
            except KafkaException as e:
                if e.args[0].code() in (
                    KafkaError.REQUEST_TIMED_OUT,
                    KafkaError.NOT_COORDINATOR,
                    KafkaError._WAIT_COORD,
                ):
                    logger.warning("Commit failed: %s (%d retries)", e, retries)
                    if retries <= 0:
                        raise
                    retries -= 1
                    time.sleep(1)
                    continue
                else:
                    raise

        if self.commit_log_topic:
            for item in offsets:
                if item.offset in self.LOGICAL_OFFSETS:
                    logger.debug(
                        "Skipped publishing logical offset (%r) to commit log for %s/%s",
                        item.offset,
                        item.topic,
                        item.partition,
                    )
                    continue
                elif item.offset < 0:
                    logger.warning(
                        "Found unexpected negative offset (%r) after commit for %s/%s",
                        item.offset,
                        item.topic,
                        item.partition,
                    )

                self.producer.produce(
                    self.commit_log_topic,
                    key=f"{item.topic}:{item.partition}:{self.group_id}".encode(),
                    value=f"{item.offset}".encode(),
                    on_delivery=self._commit_message_delivery_callback,
                )
