import styled from '@emotion/styled';

import {DataSection} from 'sentry/components/events/styles';
import GlobalAppStoreConnectUpdateAlert from 'sentry/components/globalAppStoreConnectUpdateAlert';
import {space} from 'sentry/styles/space';
import {Group, Project} from 'sentry/types';
import {Event} from 'sentry/types/event';
import {useLocation} from 'sentry/utils/useLocation';
import useOrganization from 'sentry/utils/useOrganization';
import {GroupEventCarousel} from 'sentry/views/issueDetails/groupEventCarousel';
import {OpenAIFixSuggestionPanel} from 'sentry/views/issueDetails/openAIFixSuggestion/openAIFixSuggestionPanel';

import QuickTrace from './quickTrace';

type GroupEventHeaderProps = {
  event: Event;
  group: Group;
  project: Project;
  hasReplay?: boolean;
};

const GroupEventHeader = ({event, group, project}: GroupEventHeaderProps) => {
  const location = useLocation();
  const organization = useOrganization();

  return (
    <DataSection>
      <GroupEventCarousel group={group} event={event} projectSlug={project.slug} />
      <OpenAIFixSuggestionPanel projectSlug={project.slug} eventID={event.eventID} />
      <QuickTrace
        event={event}
        group={group}
        organization={organization}
        location={location}
      />
      <StyledGlobalAppStoreConnectUpdateAlert
        project={project}
        organization={organization}
      />
    </DataSection>
  );
};

const StyledGlobalAppStoreConnectUpdateAlert = styled(GlobalAppStoreConnectUpdateAlert)`
  margin: ${space(0.5)} 0;
`;

export default GroupEventHeader;
